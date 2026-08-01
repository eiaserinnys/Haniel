"""Boundary tests for permits, manual-only retry, and server terminal authority."""

import asyncio
from datetime import datetime, timedelta, timezone

from haniel_orch.deploy_attempt_coordinator import DeployAttemptCoordinator, PlanRejected
from haniel_orch.event_store import EventStore
from haniel_orch.protocol import (
    AcceptedDeployAttemptAck,
    DeployApproval,
    DeployPlanProbe,
    DeployPlanProposal,
    DeployStatus,
    RejectedDeployAttemptAck,
    RepoReconciliation,
)


def deadline() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()


async def seed(store: EventStore, deploy_id: str = "n:r:main:target") -> dict:
    await store.create_deploy_event(
        deploy_id=deploy_id,
        node_id="n",
        repo="r",
        branch="main",
        commits=["target change"],
        affected_services=["svc"],
        diff_stat=None,
        detected_at="2026-08-01T00:00:00Z",
        target_head="target",
    )
    event = await store.get_deploy_event(deploy_id)
    assert event is not None
    return event


def auto_request(attempt_id: str = "auto-1") -> RepoReconciliation:
    return RepoReconciliation(
        phase="attempt_started",
        deploy_id="n:r:main:target",
        node_id="n",
        repo="r",
        branch="main",
        local_head="old",
        remote_head="target",
        orchestrator_attempt_id=attempt_id,
    )


async def wait_for_send(harness: "Harness") -> object:
    for _ in range(100):
        if harness.sent:
            return harness.sent[-1][1]
        await asyncio.sleep(0.001)
    raise AssertionError("coordinator did not send a message")


class Harness:
    def __init__(self, store: EventStore) -> None:
        self.sent: list[tuple[str, object]] = []
        self.sent_generations: list[str] = []
        self.broadcasts: list[dict] = []

        async def send(node_id: str, generation: str, message: object) -> bool:
            self.sent.append((node_id, message))
            self.sent_generations.append(generation)
            return True

        async def broadcast(message: dict) -> None:
            self.broadcasts.append(message)

        self.coordinator = DeployAttemptCoordinator(
            store,
            send,
            broadcast,
            probe_timeout_sec=60,
            attempt_timeout_sec=60,
        )
        self.generation = "g1"
        self.coordinator._generations["n"] = self.generation

    async def close(self) -> None:
        for timer in self.coordinator._timers.values():
            timer.cancel()
        await asyncio.gather(
            *self.coordinator._timers.values(), return_exceptions=True
        )


async def make_retry(store: EventStore, generation: str) -> None:
    assert store.attempts is not None
    await store.attempts.begin_normal_attempt(
        orchestrator_attempt_id="failed-source",
        deploy_id="n:r:main:target",
        connection_generation=generation,
        current_generation=generation,
        source="manual_single",
        approved_by="director",
        deadline_at=deadline(),
    )
    await store.attempts.fail_active_attempt(
        "failed-source",
        kind="deploy_result_failed",
        stage="execution",
        reason="deploy_result_failed",
        error="hook failed",
    )


class TestAutoPermission:
    async def test_new_change_begins_before_accepted_ack(self, store: EventStore):
        await seed(store)
        harness = Harness(store)
        try:
            await harness.coordinator.handle_auto_request(auto_request())
            probe = harness.sent[-1][1]
            assert isinstance(probe, DeployPlanProbe)
            assert await store.attempts.get_active_attempts() == []

            await harness.coordinator.handle_proposal(
                DeployPlanProposal(
                    mode="execute",
                    probe_id=probe.probe_id,
                    connection_generation=harness.generation,
                    deploy_id=probe.deploy_id,
                    node_id="n",
                    repo="r",
                    branch="main",
                    target_head="target",
                    current_head="old",
                    reason="normal_pull",
                    fingerprint="fp-auto",
                )
            )

            ack = harness.sent[-1][1]
            assert isinstance(ack, AcceptedDeployAttemptAck)
            assert ack.requested_orchestrator_attempt_id == "auto-1"
            active = await store.attempts.get_active_attempts()
            assert active[0]["orchestrator_attempt_id"] == "auto-1"
            assert active[0]["approved_by"] == "system:auto"
            assert (await store.get_deploy_event(probe.deploy_id))["status"] == "deploying"
        finally:
            await harness.close()

    async def test_retry_required_is_rejected_without_probe_attempt_or_history(
        self, store: EventStore
    ):
        await seed(store)
        harness = Harness(store)
        try:
            await make_retry(store, harness.generation)
            before = await store.get_deploy_history()

            await harness.coordinator.handle_auto_request(auto_request("auto-retry"))

            ack = harness.sent[-1][1]
            assert isinstance(ack, RejectedDeployAttemptAck)
            assert ack.requested_orchestrator_attempt_id == "auto-retry"
            assert ack.error == "retry_requires_manual_approval"
            assert await store.attempts.get_active_probes() == []
            assert await store.attempts.get_active_attempts() == []
            assert await store.get_deploy_history() == before
            assert (await store.get_deploy_event("n:r:main:target"))["status"] == "pending"
        finally:
            await harness.close()

    async def test_retry_marker_race_still_returns_explicit_rejection(
        self, store: EventStore, monkeypatch
    ):
        await seed(store)
        harness = Harness(store)
        attempts = store.attempts
        assert attempts is not None

        async def marker_not_seen(_deploy_id: str) -> bool:
            return False

        async def marker_won_before_probe(**_kwargs):
            raise PermissionError("retry_requires_manual_approval")

        monkeypatch.setattr(attempts, "has_retry_requirement", marker_not_seen)
        monkeypatch.setattr(attempts, "create_probe", marker_won_before_probe)
        try:
            await harness.coordinator.handle_auto_request(auto_request("racing-auto"))

            ack = harness.sent[-1][1]
            assert isinstance(ack, RejectedDeployAttemptAck)
            assert ack.requested_orchestrator_attempt_id == "racing-auto"
            assert ack.error == "retry_requires_manual_approval"
        finally:
            await harness.close()


class TestManualRetryPreflight:
    async def test_marker_created_between_check_and_begin_switches_to_preflight(
        self, store: EventStore, monkeypatch
    ):
        event = await seed(store)
        harness = Harness(store)
        attempts = store.attempts
        assert attempts is not None
        try:
            await make_retry(store, harness.generation)

            async def stale_marker_read(_deploy_id: str) -> bool:
                return False

            monkeypatch.setattr(
                attempts, "has_retry_requirement", stale_marker_read
            )
            approval_task = asyncio.create_task(
                harness.coordinator.approve_manual(
                    event, approved_by="director", source="manual_single"
                )
            )
            probe = await wait_for_send(harness)

            assert isinstance(probe, DeployPlanProbe)
            await harness.coordinator.handle_proposal(
                DeployPlanProposal(
                    mode="fail_closed",
                    probe_id=probe.probe_id,
                    connection_generation=harness.generation,
                    deploy_id=probe.deploy_id,
                    node_id="n",
                    repo="r",
                    branch="main",
                    target_head="target",
                    current_head="target",
                    reason="journal_missing",
                    error="test closed",
                    fingerprint="fp-closed",
                )
            )
            try:
                await approval_task
            except PlanRejected as exc:
                assert exc.status_code == 422
            else:
                raise AssertionError("retry race bypassed preflight")
        finally:
            await harness.close()


class TestConnectionGenerationAuthority:
    async def test_late_g1_proposal_after_g2_reconnect_is_stale_without_begin(
        self, store: EventStore
    ):
        await seed(store)
        harness = Harness(store)
        try:
            await harness.coordinator.handle_auto_request(auto_request("g1-request"))
            probe = harness.sent[-1][1]
            old_generation = harness.generation

            new_generation = await harness.coordinator.register_connection("n")
            assert new_generation != old_generation
            await harness.coordinator.handle_proposal(
                DeployPlanProposal(
                    mode="execute",
                    probe_id=probe.probe_id,
                    connection_generation=old_generation,
                    deploy_id=probe.deploy_id,
                    node_id="n",
                    repo="r",
                    branch="main",
                    target_head="target",
                    current_head="old",
                    reason="normal_pull",
                    fingerprint="late-g1",
                )
            )

            assert await store.attempts.get_active_attempts() == []
            assert len(harness.sent) == 1
            row = next(
                item for item in await store.get_deploy_history()
                if item["deploy_id"] == f"preflight:{probe.probe_id}"
            )
            assert (
                row["terminal_kind"],
                row["terminal_stage"],
                row["terminal_reason"],
            ) == (
                "preflight_stale",
                "connection_registration",
                "connection_generation_changed",
            )
        finally:
            await harness.close()

    async def test_begin_and_approval_send_are_serialized_before_reconnect(
        self, store: EventStore
    ):
        await seed(store)
        harness = Harness(store)
        try:
            await harness.coordinator.handle_auto_request(auto_request("serialized"))
            probe = harness.sent[-1][1]
            approval_entered = asyncio.Event()
            release_approval = asyncio.Event()

            async def blocking_send(node_id: str, generation: str, message: object) -> bool:
                harness.sent.append((node_id, message))
                harness.sent_generations.append(generation)
                if isinstance(message, AcceptedDeployAttemptAck):
                    approval_entered.set()
                    await release_approval.wait()
                return True

            harness.coordinator._send = blocking_send
            proposal_task = asyncio.create_task(
                harness.coordinator.handle_proposal(
                    DeployPlanProposal(
                        mode="execute",
                        probe_id=probe.probe_id,
                        connection_generation=harness.generation,
                        deploy_id=probe.deploy_id,
                        node_id="n",
                        repo="r",
                        branch="main",
                        target_head="target",
                        current_head="old",
                        reason="normal_pull",
                        fingerprint="serialized-fp",
                    )
                )
            )
            await approval_entered.wait()
            reconnect_task = asyncio.create_task(
                harness.coordinator.register_connection("n")
            )
            await asyncio.sleep(0)
            assert not reconnect_task.done()

            release_approval.set()
            await proposal_task
            new_generation = await reconnect_task

            assert harness.sent_generations[-1] == harness.generation
            assert new_generation != harness.generation
            attempts = await store.attempts.get_active_attempts()
            assert attempts[0]["connection_generation"] == harness.generation
        finally:
            await harness.close()

    async def test_repeated_reconnect_terminalizes_each_prior_probe_once(
        self, store: EventStore
    ):
        await seed(store)
        harness = Harness(store)
        try:
            probe_ids = []
            for index in range(2):
                await harness.coordinator.handle_auto_request(
                    auto_request(f"reconnect-{index}")
                )
                probe_ids.append(harness.sent[-1][1].probe_id)
                await harness.coordinator.register_connection("n")

            history = await store.get_deploy_history()
            for probe_id in probe_ids:
                rows = [
                    item for item in history
                    if item["deploy_id"] == f"preflight:{probe_id}"
                ]
                assert len(rows) == 1
                assert rows[0]["terminal_reason"] == "connection_generation_changed"
        finally:
            await harness.close()

    async def test_disconnect_new_canonical_and_deadline_race_materializes_once(
        self, store: EventStore
    ):
        await seed(store)
        harness = Harness(store)
        try:
            await harness.coordinator.handle_auto_request(auto_request("racing"))
            probe = harness.sent[-1][1]
            await asyncio.gather(
                harness.coordinator.register_connection("n"),
                store.create_deploy_event(
                    deploy_id="n:r:main:newer",
                    node_id="n",
                    repo="r",
                    branch="main",
                    commits=["newer change"],
                    affected_services=["svc"],
                    diff_stat=None,
                    detected_at="2026-08-02T00:00:00Z",
                    target_head="newer",
                ),
                store.attempts.terminalize_preflight(
                    probe.probe_id,
                    kind="preflight_timeout",
                    stage="deadline",
                    reason="probe_timeout",
                    error="probe timed out",
                ),
            )

            rows = [
                item for item in await store.get_deploy_history()
                if item["deploy_id"] == f"preflight:{probe.probe_id}"
            ]
            assert len(rows) == 1
            assert await store.attempts.get_active_attempts() == []
        finally:
            await harness.close()

class TestManualRetryPreflightContinuation:
    async def test_manual_retry_waits_for_proposal_then_sends_fixed_mode_approval(
        self, store: EventStore
    ):
        event = await seed(store)
        harness = Harness(store)
        try:
            await make_retry(store, harness.generation)
            approval_task = asyncio.create_task(
                harness.coordinator.approve_manual(
                    event, approved_by="director", source="manual_single"
                )
            )
            probe = await wait_for_send(harness)
            assert isinstance(probe, DeployPlanProbe)

            await harness.coordinator.handle_proposal(
                DeployPlanProposal(
                    mode="execute",
                    probe_id=probe.probe_id,
                    connection_generation=harness.generation,
                    deploy_id=probe.deploy_id,
                    node_id="n",
                    repo="r",
                    branch="main",
                    target_head="target",
                    current_head="target",
                    reason="legacy_retry",
                    fingerprint="fp-manual",
                )
            )

            orchestrator_attempt_id = await approval_task
            approval = harness.sent[-1][1]
            assert isinstance(approval, DeployApproval)
            assert approval.orchestrator_attempt_id == orchestrator_attempt_id
            assert approval.execution_mode == "execute"
            assert approval.preflight_fingerprint == "fp-manual"
        finally:
            await harness.close()

    async def test_fail_closed_disconnect_and_deadline_materialize_once(
        self, store: EventStore
    ):
        event = await seed(store)
        harness = Harness(store)
        try:
            await make_retry(store, harness.generation)
            approval_task = asyncio.create_task(
                harness.coordinator.approve_manual(
                    event, approved_by="director", source="manual_single"
                )
            )
            probe = await wait_for_send(harness)
            await harness.coordinator.handle_proposal(
                DeployPlanProposal(
                    mode="fail_closed",
                    probe_id=probe.probe_id,
                    connection_generation=harness.generation,
                    deploy_id=probe.deploy_id,
                    node_id="n",
                    repo="r",
                    branch="main",
                    target_head="target",
                    current_head="target",
                    reason="journal_missing",
                    error="manifest retry journal is missing",
                    fingerprint="fp-fail",
                )
            )
            await harness.coordinator.disconnect("n", harness.generation)
            assert not await store.attempts.terminalize_preflight(
                probe.probe_id,
                kind="preflight_timeout",
                stage="deadline",
                reason="timeout",
                error="late timeout",
            )
            try:
                await approval_task
            except Exception:
                pass
            rows = [
                row
                for row in await store.get_deploy_history()
                if row["deploy_id"] == f"preflight:{probe.probe_id}"
            ]
            assert len(rows) == 1
            assert rows[0]["terminal_kind"] == "preflight_fail_closed"
            assert rows[0]["error"] == "manifest retry journal is missing"
            assert (await store.get_deploy_event(probe.deploy_id))["status"] == "pending"
        finally:
            await harness.close()

    async def test_store_terminal_releases_manual_preflight_waiter(
        self, store: EventStore
    ):
        event = await seed(store)
        harness = Harness(store)
        try:
            await make_retry(store, harness.generation)
            approval_task = asyncio.create_task(
                harness.coordinator.approve_manual(
                    event, approved_by="director", source="manual_single"
                )
            )
            await wait_for_send(harness)
            await store.update_deploy_status(
                event["deploy_id"], DeployStatus.REJECTED, reject_reason="operator"
            )

            await harness.coordinator.resolve_terminal_canonicals(
                {event["deploy_id"]}, message="deploy was rejected during preflight"
            )

            try:
                await approval_task
            except PlanRejected as exc:
                assert exc.status_code == 409
                assert str(exc) == "deploy was rejected during preflight"
            else:
                raise AssertionError("terminal preflight waiter was not released")
        finally:
            await harness.close()
