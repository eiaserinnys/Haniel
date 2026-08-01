"""Contract tests for v16 durable deploy attempts and retry state."""

import sqlite3

from datetime import datetime, timedelta, timezone

from haniel_orch.event_store import EventStore
from haniel_orch.protocol import (
    DeployPlanProposal,
    DeployStatus,
    ManifestRecoveryEvidence,
)


def deadline(seconds: int = 60) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


async def canonical(store: EventStore, deploy_id: str = "n:r:main:target") -> dict:
    await store.create_deploy_event(
        deploy_id=deploy_id,
        node_id="n",
        repo="r",
        branch="main",
        commits=["target change"],
        affected_services=["svc"],
        diff_stat="1 file",
        detected_at="2026-08-01T00:00:00Z",
        target_head="target",
    )
    event = await store.get_deploy_event(deploy_id)
    assert event is not None
    return event


async def begin(store: EventStore, attempt_id: str = "a1") -> None:
    assert store.attempts is not None
    await store.attempts.begin_normal_attempt(
        orchestrator_attempt_id=attempt_id,
        deploy_id="n:r:main:target",
        connection_generation="g1",
        source="manual_single",
        approved_by="director",
        deadline_at=deadline(),
    )


class TestNormalFinalization:
    async def test_raw_success_waits_for_settled_equal(self, store: EventStore):
        await canonical(store)
        await begin(store)
        attempts = store.attempts
        assert attempts is not None

        raw = await attempts.record_result(
            deploy_id="n:r:main:target",
            orchestrator_attempt_id="a1",
            connection_generation="g1",
            status="success",
            error=None,
            duration_ms=321,
        )
        assert raw["status"] == "recorded"
        assert (await store.get_deploy_event("n:r:main:target"))["status"] == "deploying"

        settled = await attempts.record_settled(
            deploy_id="n:r:main:target",
            orchestrator_attempt_id="a1",
            connection_generation="g1",
            local_head="target",
            remote_head="target",
        )
        assert settled["status"] == "success"
        event = await store.get_deploy_event("n:r:main:target")
        assert event["status"] == "success"
        assert event["duration_ms"] == 321

    async def test_failure_equal_reopens_retryable_pending(self, store: EventStore):
        await canonical(store)
        await begin(store)
        attempts = store.attempts
        assert attempts is not None
        result = await attempts.record_result(
            deploy_id="n:r:main:target",
            orchestrator_attempt_id="a1",
            connection_generation="g1",
            status="failed",
            error="hook failed",
            duration_ms=12,
        )
        assert result["status"] == "failed"
        event = await store.get_deploy_event("n:r:main:target")
        assert event["status"] == "pending"
        assert event["error"] is None
        retry = await attempts.get_retry_requirement(event["deploy_id"])
        assert retry["source_orchestrator_attempt_id"] == "a1"
        history = await store.get_deploy_history()
        attempt = next(row for row in history if row["deploy_id"] == "attempt:a1")
        assert attempt["error"] == "hook failed"
        assert attempt["duration_ms"] == 12

    async def test_success_result_with_head_mismatch_is_failed_and_retryable(
        self, store: EventStore
    ):
        await canonical(store)
        await begin(store)
        attempts = store.attempts
        assert attempts is not None
        await attempts.record_result(
            deploy_id="n:r:main:target",
            orchestrator_attempt_id="a1",
            connection_generation="g1",
            status="success",
            error=None,
            duration_ms=1,
        )
        result = await attempts.record_settled(
            deploy_id="n:r:main:target",
            orchestrator_attempt_id="a1",
            connection_generation="g1",
            local_head="other",
            remote_head="target",
        )
        assert result["status"] == "failed"
        assert (await store.get_deploy_event("n:r:main:target"))["status"] == "pending"
        history = await store.get_deploy_history()
        assert "settled HEAD mismatch" in next(
            row["error"] for row in history if row["deploy_id"] == "attempt:a1"
        )

    async def test_settled_from_other_connection_generation_is_ignored(
        self, store: EventStore
    ):
        await canonical(store)
        await begin(store)
        attempts = store.attempts
        assert attempts is not None
        await attempts.record_result(
            deploy_id="n:r:main:target",
            orchestrator_attempt_id="a1",
            connection_generation="g1",
            status="success",
            error=None,
            duration_ms=1,
        )

        stale = await attempts.record_settled(
            deploy_id="n:r:main:target",
            orchestrator_attempt_id="a1",
            connection_generation="g2",
            local_head="target",
            remote_head="target",
        )

        assert stale["status"] == "ignored"
        assert (await store.get_deploy_event("n:r:main:target"))["status"] == "deploying"

    async def test_late_attempt_a_cannot_mutate_retry_b(self, store: EventStore):
        await canonical(store)
        await begin(store, "a")
        attempts = store.attempts
        assert attempts is not None
        await attempts.fail_active_attempt("a", kind="attempt_timeout", error="timeout")

        probe = await attempts.create_probe(
            probe_id="p-b",
            deploy_id="n:r:main:target",
            connection_generation="g1",
            deadline_at=deadline(),
            manual_retry=True,
        )
        proposal = DeployPlanProposal(
            mode="execute",
            probe_id="p-b",
            connection_generation="g1",
            deploy_id="n:r:main:target",
            node_id="n",
            repo="r",
            branch="main",
            target_head="target",
            current_head="target",
            reason="legacy_retry",
            fingerprint="fp-b",
        )
        begun = await attempts.record_proposal_and_begin(
            proposal,
            orchestrator_attempt_id="b",
            source="manual_single",
            approved_by="director",
            deadline_at=deadline(),
        )
        assert begun["status"] == "begun"
        late = await attempts.record_result(
            deploy_id="n:r:main:target",
            orchestrator_attempt_id="a",
            connection_generation="g1",
            status="success",
            error=None,
            duration_ms=99,
        )
        assert late["status"] == "ignored"
        late_failure = await attempts.record_result(
            deploy_id="n:r:main:target",
            orchestrator_attempt_id="a",
            connection_generation="g1",
            status="failed",
            error="late failure",
            duration_ms=100,
        )
        late_settled = await attempts.record_settled(
            deploy_id="n:r:main:target",
            orchestrator_attempt_id="a",
            connection_generation="g1",
            local_head="target",
            remote_head="target",
        )
        late_timeout = await attempts.fail_active_attempt(
            "a", kind="attempt_timeout", error="late timeout"
        )
        assert {
            late_failure["status"],
            late_settled["status"],
            late_timeout["status"],
        } == {"ignored"}
        active = await attempts.get_active_attempts()
        assert [row["orchestrator_attempt_id"] for row in active] == ["b"]
        assert probe["source_orchestrator_attempt_id"] == "a"

    async def test_newer_canonical_wins_if_old_attempt_fails_after_creation(
        self, store: EventStore
    ):
        await canonical(store)
        await begin(store, "old-attempt")
        await store.create_deploy_event(
            deploy_id="n:r:main:new-target",
            node_id="n",
            repo="r",
            branch="main",
            commits=["new target"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-08-02T00:00:00Z",
            target_head="new-target",
        )
        attempts = store.attempts
        assert attempts is not None

        result = await attempts.fail_active_attempt(
            "old-attempt", kind="deploy_result_failed", error="old failed late"
        )

        assert result["status"] == "superseded"
        assert (await store.get_deploy_event("n:r:main:target"))["status"] == "rejected"
        assert (await store.get_deploy_event("n:r:main:new-target"))["status"] == "pending"
        assert not await attempts.has_retry_requirement("n:r:main:target")

    async def test_new_commit_supersedes_reopened_retry_and_cleans_marker(
        self, store: EventStore
    ):
        await canonical(store)
        await begin(store, "failed-first")
        attempts = store.attempts
        assert attempts is not None
        await attempts.fail_active_attempt(
            "failed-first", kind="deploy_result_failed", error="failed"
        )
        assert await attempts.has_retry_requirement("n:r:main:target")

        await store.create_deploy_event(
            deploy_id="n:r:main:new-target",
            node_id="n",
            repo="r",
            branch="main",
            commits=["new target"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-08-02T00:00:00Z",
            target_head="new-target",
        )

        assert (await store.get_deploy_event("n:r:main:target"))["status"] == "rejected"
        assert not await attempts.has_retry_requirement("n:r:main:target")


class TestPreflightAuthority:
    async def test_first_preflight_terminal_wins_one_history_row(self, store: EventStore):
        await canonical(store)
        attempts = store.attempts
        assert attempts is not None
        await attempts.create_probe(
            probe_id="p1",
            deploy_id="n:r:main:target",
            connection_generation="g1",
            deadline_at=deadline(),
            manual_retry=False,
            requested_orchestrator_attempt_id="requested-a",
        )
        assert await attempts.terminalize_preflight(
            "p1",
            kind="preflight_disconnected",
            stage="connection",
            reason="disconnect",
            error="disconnected",
        )
        assert not await attempts.terminalize_preflight(
            "p1",
            kind="preflight_timeout",
            stage="deadline",
            reason="timeout",
            error="timeout",
        )
        history = await store.get_deploy_history()
        rows = [row for row in history if row["deploy_id"] == "preflight:p1"]
        assert len(rows) == 1
        assert rows[0]["terminal_kind"] == "preflight_disconnected"
        assert (await store.get_deploy_event("n:r:main:target"))["status"] == "pending"

    async def test_auto_retry_is_rejected_before_probe_creation(self, store: EventStore):
        await canonical(store)
        await begin(store)
        attempts = store.attempts
        assert attempts is not None
        await attempts.fail_active_attempt("a1", kind="failed", error="boom")
        try:
            await attempts.create_probe(
                probe_id="must-not-exist",
                deploy_id="n:r:main:target",
                connection_generation="g1",
                deadline_at=deadline(),
                manual_retry=False,
                requested_orchestrator_attempt_id="auto-a",
            )
        except PermissionError as exc:
            assert str(exc) == "retry_requires_manual_approval"
        else:
            raise AssertionError("auto retry unexpectedly created a probe")
        assert await attempts.get_active_probes() == []


class TestEvidenceRecovery:
    async def test_strict_recovery_keeps_timeout_history_and_adds_success(
        self, store: EventStore
    ):
        await store.create_deploy_event(
            deploy_id="n:r:main:target",
            node_id="n",
            repo="r",
            branch="main",
            commits=["target change"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-08-01T00:00:00Z",
            target_head="target",
            deployment_kind="manifest",
            expected_manifest_identity="release.json",
            expected_manifest_digest="digest",
        )
        await begin(store, "source")
        attempts = store.attempts
        assert attempts is not None
        await attempts.fail_active_attempt("source", kind="attempt_timeout", error="lost")
        await attempts.create_probe(
            probe_id="recovery-probe",
            deploy_id="n:r:main:target",
            connection_generation="g2",
            deadline_at=deadline(),
            manual_retry=True,
        )
        proposal = DeployPlanProposal(
            mode="evidence_recovery",
            probe_id="recovery-probe",
            connection_generation="g2",
            deploy_id="n:r:main:target",
            node_id="n",
            repo="r",
            branch="main",
            target_head="target",
            current_head="target",
            journal_status="success",
            journal_attempt_id="journal-1",
            linked_orchestrator_attempt_id="source",
            journal_target_head="target",
            manifest_identity="release.json",
            manifest_digest="digest",
            journal_completed_at="2026-08-01T00:01:00Z",
            reason="durable_local_success",
            fingerprint="recovery-fp",
        )
        result = await attempts.record_proposal_and_begin(
            proposal,
            orchestrator_attempt_id="recovery",
            source="manual_single",
            approved_by="director",
            deadline_at=deadline(),
        )
        assert result["status"] == "begun"
        recovered = await attempts.record_recovery_evidence(
            ManifestRecoveryEvidence(
                deploy_id="n:r:main:target",
                orchestrator_attempt_id="recovery",
                source_orchestrator_attempt_id="source",
                journal_attempt_id="journal-1",
                connection_generation="g2",
                node_id="n",
                repo="r",
                branch="main",
                target_head="target",
                current_head="target",
                manifest_identity="release.json",
                manifest_digest="digest",
                journal_status="success",
                journal_completed_at="2026-08-01T00:01:00Z",
            )
        )
        assert recovered["status"] == "success"
        history = await store.get_deploy_history()
        ids = {row["deploy_id"] for row in history}
        assert {"attempt:source", "n:r:main:target"} <= ids
        assert "attempt:recovery" not in ids
        canonical_row = next(
            row for row in history if row["deploy_id"] == "n:r:main:target"
        )
        assert canonical_row["orchestrator_attempt_id"] == "recovery"
        assert canonical_row["attempt_outcome"] == "evidence_recovered_success"
        assert not await attempts.has_retry_requirement("n:r:main:target")


class TestSchemaMigration:
    async def test_existing_failed_is_history_only_and_new_state_starts_empty(
        self, tmp_path
    ):
        db_path = tmp_path / "legacy.sqlite3"
        connection = sqlite3.connect(db_path)
        connection.executescript(
            """
            CREATE TABLE deploy_events (
                deploy_id TEXT PRIMARY KEY,
                node_id TEXT NOT NULL,
                repo TEXT NOT NULL,
                branch TEXT NOT NULL,
                status TEXT NOT NULL,
                commits_json TEXT NOT NULL,
                affected_services_json TEXT NOT NULL,
                diff_stat TEXT,
                detected_at TEXT NOT NULL,
                approved_by TEXT,
                reject_reason TEXT,
                error TEXT,
                duration_ms INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO deploy_events VALUES
              ('failed-old','n','r','main','failed','[]','[]',NULL,
               '2026-07-01T00:00:00Z',NULL,NULL,'old failure',10,
               '2026-07-01T00:00:00Z','2026-07-01T00:01:00Z'),
              ('pending-old','n','other','main','pending','[]','[]',NULL,
               '2026-07-02T00:00:00Z',NULL,NULL,NULL,NULL,
               '2026-07-02T00:00:00Z','2026-07-02T00:00:00Z');
            """
        )
        connection.commit()
        connection.close()

        migrated = EventStore(str(db_path))
        await migrated.initialize()
        try:
            failed = await migrated.get_deploy_event("failed-old")
            pending = await migrated.get_deploy_event("pending-old")
            assert failed["status"] == "failed"
            assert pending["status"] == "pending"
            assert migrated.attempts is not None
            assert await migrated.attempts.get_active_attempts() == []
            assert await migrated.attempts.get_active_probes() == []
            assert not await migrated.attempts.has_retry_requirement("failed-old")
            assert {row["deploy_id"] for row in await migrated.get_deploy_history()} >= {
                "failed-old",
                "pending-old",
            }
        finally:
            await migrated.close()


class TestRetryMarkerCleanup:
    async def test_explicit_reject_cleans_marker(self, store: EventStore):
        await canonical(store)
        await begin(store)
        attempts = store.attempts
        assert attempts is not None
        await attempts.fail_active_attempt("a1", kind="failed", error="boom")

        await store.update_deploy_status(
            "n:r:main:target", DeployStatus.REJECTED, reject_reason="operator"
        )

        assert not await attempts.has_retry_requirement("n:r:main:target")

    async def test_renamed_node_cleanup_cleans_marker(self, store: EventStore):
        await canonical(store)
        await begin(store)
        attempts = store.attempts
        assert attempts is not None
        await attempts.fail_active_attempt("a1", kind="failed", error="boom")

        rejected = await store.reject_pending_deploys_for_nodes(
            ["n"], "node renamed"
        )

        assert [row["deploy_id"] for row in rejected] == ["n:r:main:target"]
        assert not await attempts.has_retry_requirement("n:r:main:target")
