"""믿음 영역이 저장소와 코디네이터에서 걷는 길(카드 #1259 PR 2b-2): 받았다, 시작했다, 믿음별 마감, 끊김의 유예,
hello 대조, 「실행하지 않음」 답, 보내기 실패, 「종결을 받았다」 답, 요청 줄이 없을 때 온 실패(G12), 계획 답의 실패 저널,
계보의 「안에 있는가」, 재기동 복원, 받기 함수와 코디네이터의 일치.

`test_repro_`로 시작하는 것은 2b-1 판(바꾸기 전 커밋 e8bf06b)에서 실패해야 하는 재현 시험이다(어긋남 6, 9, 10, design.md 348).
판 없는 시도(node_protocol NULL)는 2a와 2b-1 규칙 그대로임을 `test_legacy_`가 본다.
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from test_deploy_attempt_coordinator import Harness, auto_request, seed, wait_for_send
from test_inspection_lock import future_deadline, lineage_of, marker_of, node_report, raw
try:
    from haniel_orch.deploy_attempt_intake import (
        CLOSURE_EXECUTION_UNKNOWN,
        CLOSURE_FAILED,
        CLOSURE_NOT_EXECUTED,
        DEADLINE_ACK,
        DEADLINE_GRACE,
        DEADLINE_LEASE,
        DEADLINE_NONE,
        intake_deadline,
        intake_hello,
        intake_progress,
        intake_received,
        intake_send_failed,
        intake_terminal,
    )
except ImportError:  # 2b-1 판(재현 시험을 돌릴 때): 값 이름만 둔다. 받기 함수를 쓰는 시험은 그 자리에서 실패한다
    CLOSURE_EXECUTION_UNKNOWN, CLOSURE_FAILED, CLOSURE_NOT_EXECUTED = "execution_unknown", "failed", "not_executed"
    DEADLINE_ACK, DEADLINE_GRACE, DEADLINE_LEASE, DEADLINE_NONE = "ack", "grace", "lease", "none"
    intake_deadline = intake_hello = intake_progress = intake_received = intake_send_failed = intake_terminal = None
from haniel_orch.event_store import EventStore
from haniel_orch import protocol as _protocol
from haniel_orch.protocol import (
    AcceptedDeployAttemptAck,
    DeployApproval,
    DeployAttemptTerminal,
    DeployPlanProbe,
    DeployPlanProposal,
    DeployProgress,
    DeployReportAck,
    DeployResult,
    ManifestRecoveryEvidence,
    RejectedDeployAttemptAck,
    RepoReconciliation,
)

# 2b-1 판에는 새 글이 없다(재현 시험을 돌릴 때). 없으면 그 글을 쓰는 시험이 그 자리에서 실패한다
DeployAttemptReceived = getattr(_protocol, "DeployAttemptReceived", None)
DeployTerminalReceived = getattr(_protocol, "DeployTerminalReceived", type("_NoTerminalReceived", (), {}))
HeldAttempt = getattr(_protocol, "HeldAttempt", None)
from haniel_orch.statechart.definitions import OrchRequest
from haniel_orch.statechart.restore import advance

DEPLOY_ID = "n:r:main:target"
PAST = "2000-01-01T00:00:00+00:00"


# ---------------------------------------------------------------- 도우미


async def leaf4(store: EventStore, deploy_id: str = DEPLOY_ID) -> tuple:
    """(바깥 status, 요청 잎, 믿음 잎, 점검 필요 잎). 새 칸이 없는 2b-1 판에서는 믿음을 None으로 읽는다."""
    try:
        rows = await raw(store, "SELECT status, sm_status, sm_belief, sm_lock FROM deploy_events WHERE deploy_id = ?", deploy_id)
    except sqlite3.OperationalError:
        rows = await raw(store, "SELECT status, sm_status, NULL, sm_lock FROM deploy_events WHERE deploy_id = ?", deploy_id)
    return rows[0]


async def attempt_cols(store: EventStore, attempt_id: str) -> tuple:
    """(outcome, deadline_kind, closure, node_protocol, terminal_kind). 2b-1 판에서는 새 칸을 None으로 읽는다."""
    try:
        rows = await raw(
            store,
            "SELECT outcome, deadline_kind, closure, node_protocol, terminal_kind FROM deploy_attempts WHERE orchestrator_attempt_id = ?",
            attempt_id,
        )
    except sqlite3.OperationalError:
        rows = await raw(store, "SELECT outcome, NULL, NULL, NULL, terminal_kind FROM deploy_attempts WHERE orchestrator_attempt_id = ?", attempt_id)
    return rows[0]


async def begin(store: EventStore, attempt_id: str = "a1", *, protocol: int | None = 1, generation: str = "g1") -> dict:
    kwargs = {} if protocol is None else {"node_protocol": protocol}
    common = dict(
        orchestrator_attempt_id=attempt_id,
        deploy_id=DEPLOY_ID,
        connection_generation=generation,
        current_generation=generation,
        source="manual_single",
        approved_by="director",
        deadline_at=future_deadline(),
    )
    try:
        return await store.attempts.begin_normal_attempt(**common, **kwargs)
    except TypeError:
        # 2b-1 판에는 node_protocol이 없다. 판 없는 시도로 열어 재현 시험이 기대값과 다르다는 단언으로 실패하게 한다
        return await store.attempts.begin_normal_attempt(**common)


async def begin_via_probe(
    store: EventStore, attempt_id: str, *, protocol: int | None = 1, generation: str = "g1", deploy_id: str = DEPLOY_ID
) -> dict:
    """source가 있는 표시 줄의 점검 뒤 승인: 계획을 묻고 답을 받아 시도를 연다."""
    node_id, repo, branch, target = deploy_id.split(":", 3)
    probe = await store.attempts.create_probe(
        probe_id=f"p-{attempt_id}", deploy_id=deploy_id, connection_generation=generation, deadline_at=future_deadline(), manual_retry=True
    )
    assert probe["todo"] == ["send_probe"]
    plan = DeployPlanProposal(
        mode="execute", probe_id=probe["probe_id"], connection_generation=generation, deploy_id=deploy_id, node_id=node_id, repo=repo,
        branch=branch, target_head=target, current_head="old", reason="legacy_retry", fingerprint=f"fp-{attempt_id}",
    )
    common = dict(
        orchestrator_attempt_id=attempt_id, source="manual_single", approved_by="director", deadline_at=future_deadline(),
        current_generation=generation,
    )
    try:
        begun = await store.attempts.record_proposal_and_begin(plan, **common, node_protocol=protocol)
    except TypeError:
        begun = await store.attempts.record_proposal_and_begin(plan, **common)  # 2b-1 판(위 begin과 같은 까닭)
    assert begun["status"] == "begun", begun
    return begun


async def received(store: EventStore, attempt_id: str = "a1", node_id: str = "n") -> dict:
    return await store.attempts.record_received(deploy_id=DEPLOY_ID, orchestrator_attempt_id=attempt_id, node_id=node_id)


async def progress(store: EventStore, attempt_id: str = "a1") -> dict:
    now = datetime.now(timezone.utc)
    return await store.attempts.record_progress(
        deploy_id=DEPLOY_ID,
        orchestrator_attempt_id=attempt_id,
        node_id="n",
        expected_budget_sec=None,
        deadline_at=(now + timedelta(seconds=60)).isoformat(),
        now=now.isoformat(),
    )


async def expire(store: EventStore, attempt_id: str = "a1") -> dict:
    """마감 시각을 과거로 돌리고 타이머가 울린 것처럼 만기를 넣는다."""
    await raw(store, "UPDATE deploy_attempts SET deadline_at = ? WHERE orchestrator_attempt_id = ?", PAST, attempt_id)
    return await store.attempts.fail_attempt_if_deadline_due(
        attempt_id, expected_deadline_at=PAST, kind="k", stage="deadline", reason="r", error="e"
    )


def terminal(attempt_id: str = "a1", kind: str = "not_executed", generation: str = "g1") -> DeployAttemptTerminal:
    if kind == "not_executed":
        return DeployAttemptTerminal(
            kind="not_executed", stage="admission", reason="already_held", deploy_id=DEPLOY_ID,
            orchestrator_attempt_id=attempt_id, connection_generation=generation, error="held",
        )
    return DeployAttemptTerminal(
        kind="approval_revalidation_failed", stage="approval_revalidation", reason="approval_revalidation_failed",
        deploy_id=DEPLOY_ID, orchestrator_attempt_id=attempt_id, connection_generation=generation, error="stale",
    )


def result(attempt_id: str = "a1", status: str = "success", generation: str = "g1") -> DeployResult:
    return DeployResult(
        deploy_id=DEPLOY_ID, node_id="n", status=status, error=None if status == "success" else "boom",
        duration_ms=5, orchestrator_attempt_id=attempt_id, connection_generation=generation,
    )


def settled(attempt_id: str = "a1", local_head: str = "target", generation: str = "g1") -> RepoReconciliation:
    return RepoReconciliation(
        phase="settled", deploy_id=DEPLOY_ID, node_id="n", repo="r", branch="main", local_head=local_head,
        remote_head="target", orchestrator_attempt_id=attempt_id, connection_generation=generation,
    )


def p1(harness: Harness) -> Harness:
    """노드 n의 연결이 판 1이라고 말하는 하니스."""
    harness.coordinator._node_protocol = lambda _node_id: 1
    return harness


def sent_of(harness: Harness, cls) -> list:
    return [message for _node, message in harness.sent if isinstance(message, cls)]


# ---------------------------------------------------------------- 시도 열기: 판과 마감 종류


async def test_a_protocol_one_attempt_opens_with_the_short_deadline_and_a_legacy_one_with_the_lease(store: EventStore):
    await seed(store)
    begun = await begin(store, "a1", protocol=1)
    assert begun["todo"] == ["send_permit"]
    assert await attempt_cols(store, "a1") == ("active", DEADLINE_ACK, None, 1, None)
    assert await leaf4(store) == ("deploying", "deploying", "b_unknown", "l_off")
    legacy = EventStore(":memory:")
    await legacy.initialize()
    try:
        await seed(legacy)
        await begin(legacy, "l1", protocol=None)
        assert await attempt_cols(legacy, "l1") == ("active", DEADLINE_LEASE, None, None, None)
        assert await leaf4(legacy) == ("deploying", "deploying", "b_unknown", "l_off")
    finally:
        await legacy.close()


async def test_the_coordinator_arms_the_short_deadline_for_a_protocol_one_node(store: EventStore):
    await seed(store)
    harness = p1(Harness(store))
    try:
        event = await store.get_deploy_event(DEPLOY_ID)
        attempt_id = await harness.coordinator.approve_manual(event, approved_by="d", source="manual_single")
        attempt = await store.attempts.get_attempt(attempt_id)
        assert attempt["deadline_kind"] == DEADLINE_ACK and attempt["node_protocol"] == 1
        assert datetime.fromisoformat(attempt["deadline_at"]) <= datetime.now(timezone.utc) + timedelta(seconds=31)
        assert isinstance(harness.sent[-1][1], DeployApproval)
        assert f"attempt:{attempt_id}" in harness.coordinator._timers
    finally:
        await harness.close()


# ---------------------------------------------------------------- 받았다, 시작했다


async def test_received_moves_the_belief_to_queued_and_stops_the_short_deadline(store: EventStore):
    await seed(store)
    await begin(store)
    assert await received(store) == {
        "status": "acked", "deploy_id": DEPLOY_ID, "orchestrator_attempt_id": "a1",
        "belief_previous": "b_unknown", "belief": "b_queued",
    }
    assert await leaf4(store) == ("deploying", "deploying", "b_queued", "l_off")
    assert (await attempt_cols(store, "a1"))[1] == DEADLINE_NONE
    # 같은 글이 또 와도 제자리다
    again = await received(store)
    assert (again["belief_previous"], again["belief"]) == ("b_queued", "b_queued")


async def test_received_is_dropped_for_another_node_a_closed_attempt_or_a_legacy_attempt(store: EventStore):
    await seed(store)
    await begin(store)
    assert (await received(store, node_id="other"))["reason"] == "identity_mismatch"
    assert (await store.attempts.record_received(deploy_id="x:r:main:t", orchestrator_attempt_id="a1", node_id="n"))["reason"] == "identity_mismatch"
    await store.attempts.fail_attempt_not_sent("a1")
    assert (await received(store))["reason"] == "terminal_attempt"
    assert await leaf4(store) == ("pending", "again", "b_none", "l_off")  # 닫힌 시도의 글은 믿음을 옮기지 않는다
    legacy = EventStore(":memory:")
    await legacy.initialize()
    try:
        await seed(legacy)
        await begin(legacy, protocol=None)
        assert (await received(legacy))["reason"] == "legacy_attempt"
        assert await leaf4(legacy) == ("deploying", "deploying", "b_unknown", "l_off")
    finally:
        await legacy.close()


async def test_the_first_progress_is_started_and_switches_the_deadline_to_the_lease(store: EventStore):
    await seed(store)
    await begin(store)
    await received(store)
    renewed = await progress(store)
    assert renewed["status"] == "renewed" and renewed["belief"] == "b_running"
    assert await leaf4(store) == ("deploying", "deploying", "b_running", "l_off")
    assert (await attempt_cols(store, "a1"))[1] == DEADLINE_LEASE
    # 판 없는 시도도 첫 진행 알림으로 시작했다가 된다(G7). 마감은 지금처럼 실행 임대다
    legacy = EventStore(":memory:")
    await legacy.initialize()
    try:
        await seed(legacy)
        await begin(legacy, protocol=None)
        assert (await progress(legacy))["belief"] == "b_running"
        assert (await attempt_cols(legacy, "a1"))[1] == DEADLINE_LEASE
    finally:
        await legacy.close()


# ---------------------------------------------------------------- 마감 (wire-v1 5절)


async def test_repro_divergence_10_the_short_deadline_closes_an_unacknowledged_permit_as_again(store: EventStore):
    """어긋남 10: 자동 허가가 닿았는지 모르면 짧은 마감이 다시 대기로 보낸다. 점검 필요는 서지 않는다."""
    await seed(store)
    await begin(store)
    expired = await expire(store)
    assert (expired["status"], expired["event"]) == ("failed", "ack_deadline")
    assert await leaf4(store) == ("pending", "again", "b_none", "l_off")
    assert await attempt_cols(store, "a1") == ("failed", DEADLINE_ACK, CLOSURE_EXECUTION_UNKNOWN, 1, "k")
    assert await marker_of(store) == ("a1", "a1", 0) and await lineage_of(store) == ["a1"]  # 계보에 더하고 source를 옮긴다


async def test_repro_divergence_6_a_queued_attempt_does_not_expire_while_the_node_stays_connected(store: EventStore):
    """어긋남 6: 줄 서 있는 동안은 세지 않는다. 옛 5분 마감이 지나도 시도는 그대로 돈다."""
    await seed(store)
    await begin(store)
    await received(store)
    expired = await expire(store)
    assert expired["status"] == "stale" and expired["deadline_kind"] == DEADLINE_NONE
    assert await leaf4(store) == ("deploying", "deploying", "b_queued", "l_off")
    assert (await attempt_cols(store, "a1"))[0] == "active"


async def test_the_grace_deadline_counts_only_while_disconnected_and_locks(store: EventStore):
    await seed(store)
    await begin(store)
    await received(store)
    graced = await store.attempts.mark_link_down("n", grace_sec=300)
    assert [g["orchestrator_attempt_id"] for g in graced] == ["a1"]
    kind = (await attempt_cols(store, "a1"))[1]
    assert kind == DEADLINE_GRACE
    assert datetime.fromisoformat(graced[0]["deadline_at"]) > datetime.now(timezone.utc) + timedelta(seconds=290)
    expired = await expire(store)
    assert expired["event"] == "grace_deadline"
    assert await leaf4(store) == ("pending", "retry", "b_none", "l_on")
    assert (await attempt_cols(store, "a1"))[2] == CLOSURE_FAILED


async def test_reconnecting_with_the_attempt_in_the_hello_list_stops_the_grace_deadline(store: EventStore):
    await seed(store)
    await begin(store)
    await received(store)
    await store.attempts.mark_link_down("n", grace_sec=300)
    held = await store.attempts.reconcile_hello("n", {"a1"})
    assert held[0]["event"] == "hello_holds" and held[0]["belief_after"] == "b_queued"
    assert (await attempt_cols(store, "a1"))[1] == DEADLINE_NONE
    assert (await expire(store))["status"] == "stale"


async def test_the_lease_deadline_after_started_locks_like_a_failure(store: EventStore):
    await seed(store)
    await begin(store)
    await progress(store)
    expired = await expire(store)
    assert expired["event"] == "lease_deadline"
    assert await leaf4(store) == ("pending", "retry", "b_none", "l_on")
    assert (await attempt_cols(store, "a1"))[2] == CLOSURE_FAILED


async def test_a_deadline_whose_kind_and_belief_disagree_is_stale_and_writes_nothing(store: EventStore):
    await seed(store)
    await begin(store)
    await progress(store)  # 시작했다, 실행 임대
    await raw(store, "UPDATE deploy_attempts SET deadline_kind = 'ack' WHERE orchestrator_attempt_id = 'a1'")  # 낡은 타이머 흉내
    assert (await expire(store))["status"] == "stale"
    assert await leaf4(store) == ("deploying", "deploying", "b_running", "l_off")


async def test_the_timer_dispatches_ack_and_grace_kinds_and_skips_none(store: EventStore):
    await seed(store)
    harness = p1(Harness(store))
    try:
        await begin(store)
        await raw(store, "UPDATE deploy_attempts SET deadline_at = ? WHERE orchestrator_attempt_id = 'a1'", PAST)
        await harness.coordinator._attempt_timeout("a1", 0)
        assert (await attempt_cols(store, "a1"))[4] == "ack_timeout"
        assert await leaf4(store) == ("pending", "again", "b_none", "l_off")
        # 줄 서 있고 붙어 있는 시도의 타이머는 울려도 아무것도 하지 않는다
        await begin_via_probe(store, "a2")
        await received(store, "a2")
        await raw(store, "UPDATE deploy_attempts SET deadline_at = ? WHERE orchestrator_attempt_id = 'a2'", PAST)
        await harness.coordinator._attempt_timeout("a2", 0)
        assert (await attempt_cols(store, "a2"))[0] == "active"
        await store.attempts.mark_link_down("n", grace_sec=300)
        await raw(store, "UPDATE deploy_attempts SET deadline_at = ? WHERE orchestrator_attempt_id = 'a2'", PAST)
        await harness.coordinator._attempt_timeout("a2", 0)
        assert (await attempt_cols(store, "a2"))[4] == "grace_timeout"
        assert await leaf4(store) == ("pending", "retry", "b_none", "l_on")
    finally:
        await harness.close()


# ---------------------------------------------------------------- hello 대조 (design.md 141-145)


@pytest.mark.parametrize(
    ("belief", "in_list", "expected_leaf", "closure"),
    [
        ("b_unknown", True, ("deploying", "deploying", "b_queued", "l_off"), None),
        ("b_queued", True, ("deploying", "deploying", "b_queued", "l_off"), None),
        ("b_running", True, ("deploying", "deploying", "b_running", "l_off"), None),
        ("b_unknown", False, ("pending", "again", "b_none", "l_off"), CLOSURE_EXECUTION_UNKNOWN),
        ("b_queued", False, ("pending", "again", "b_none", "l_off"), CLOSURE_EXECUTION_UNKNOWN),
        ("b_running", False, ("pending", "retry", "b_none", "l_on"), CLOSURE_FAILED),
    ],
)
async def test_hello_reconciliation_table_in_the_store(store: EventStore, belief, in_list, expected_leaf, closure):
    await seed(store)
    await begin(store)
    if belief in ("b_queued", "b_running"):
        await received(store)
    if belief == "b_running":
        await progress(store)
    results = await store.attempts.reconcile_hello("n", {"a1"} if in_list else set())
    assert results[0]["event"] == ("hello_holds" if in_list else "hello_missing")
    assert await leaf4(store) == expected_leaf
    assert (await attempt_cols(store, "a1"))[2] == closure
    if not in_list:
        assert (await attempt_cols(store, "a1"))[4] == "hello_missing"
        assert await lineage_of(store) == ["a1"]


async def test_hello_without_a_list_or_for_a_legacy_attempt_does_not_reconcile(store: EventStore):
    await seed(store)
    await begin(store)
    assert await store.attempts.reconcile_hello("n", None) == []
    assert await leaf4(store) == ("deploying", "deploying", "b_unknown", "l_off")
    legacy = EventStore(":memory:")
    await legacy.initialize()
    try:
        await seed(legacy)
        await begin(legacy, protocol=None)
        assert await legacy.attempts.reconcile_hello("n", set()) == []
        assert await leaf4(legacy) == ("deploying", "deploying", "b_unknown", "l_off")
    finally:
        await legacy.close()


async def test_repro_a_deploy_stuck_disconnected_is_freed_by_hello_or_by_the_grace_deadline(store: EventStore):
    """design.md 348: 끊긴 채 배포 중에 갇히는 길. 다시 붙은 hello에 없으면 다시 대기로, 돌아오지 않으면 유예 마감이 닫는다."""
    await seed(store)
    harness = p1(Harness(store))
    try:
        await begin(store)
        await received(store)
        assert await harness.disconnect_current()
        assert (await attempt_cols(store, "a1"))[1] == DEADLINE_GRACE
        assert "attempt:a1" in harness.coordinator._timers
        await harness.reconnect()
        results = await harness.coordinator.reconcile_hello("n", 1, [])
        assert results[0]["event"] == "hello_missing"
        assert await leaf4(store) == ("pending", "again", "b_none", "l_off")
        assert "attempt:a1" not in harness.coordinator._timers
    finally:
        await harness.close()


# ---------------------------------------------------------------- 「실행하지 않음」, 보내기 실패 (닫힘 일곱)


async def test_repro_divergence_10_a_declined_permit_goes_to_again_without_moving_the_source(store: EventStore):
    await seed(store)
    await begin(store, "a1")
    await store.attempts.fail_attempt_not_sent("a1")  # 첫 시도 보내기 실패: source는 빈 채 계보에만
    assert await leaf4(store) == ("pending", "again", "b_none", "l_off")
    assert await marker_of(store) == (None, "a1", 0) and await lineage_of(store) == ["a1"]
    await begin(store, "a2")
    await received(store, "a2")
    declined = await store.attempts.record_node_terminal(terminal("a2"))
    assert (declined["status"], declined["event"]) == ("failed", "declined")
    assert await leaf4(store) == ("pending", "again", "b_none", "l_off")
    assert await marker_of(store) == (None, "a2", 0) and await lineage_of(store) == ["a1", "a2"]
    assert await attempt_cols(store, "a2") == ("failed", DEADLINE_NONE, CLOSURE_NOT_EXECUTED, 1, "not_executed")


async def test_a_terminal_after_started_is_a_real_failure(store: EventStore):
    """4e: 시작했다 뒤에 온 「실행하지 않음」은 실제 실행으로 보아 점검 필요를 켠다."""
    await seed(store)
    await begin(store)
    await progress(store)
    closed = await store.attempts.record_node_terminal(terminal("a1", kind="revalidation"))
    assert closed["event"] == "attempt_failed"
    assert await leaf4(store) == ("pending", "retry", "b_none", "l_on")
    assert (await attempt_cols(store, "a1"))[2] == CLOSURE_FAILED


async def test_repro_divergence_9_a_protocol_one_send_failure_leaves_deploying_as_again(store: EventStore):
    await seed(store)
    await begin(store)
    closed = await store.attempts.fail_attempt_not_sent("a1")
    assert closed["status"] == "failed"
    assert await leaf4(store) == ("pending", "again", "b_none", "l_off")
    assert await attempt_cols(store, "a1") == ("failed", DEADLINE_ACK, CLOSURE_NOT_EXECUTED, 1, "delivery_failed")


async def test_repro_divergence_10_a_standing_lock_turns_a_no_closure_into_retry(store: EventStore):
    """P8-2: 배포 중에 선 점검 필요(노드 스스로 한 배포의 실패)는 「아니오」 닫힘을 실패 뒤 대기로 보낸다."""
    await seed(store)
    await begin(store)
    await store.record_node_deploy_report(node_report("failed", node_attempt_id="na1"))
    assert await leaf4(store) == ("deploying", "deploying", "b_unknown", "l_on")
    await store.attempts.record_node_terminal(terminal("a1"))
    assert await leaf4(store) == ("pending", "retry", "b_none", "l_on")
    assert (await attempt_cols(store, "a1"))[2] == CLOSURE_NOT_EXECUTED


async def test_a_newer_commit_supersedes_any_closure(store: EventStore):
    await seed(store)
    await begin(store)
    await store.create_deploy_event(
        deploy_id="n:r:main:newer", node_id="n", repo="r", branch="main", commits=["c"], affected_services=[],
        diff_stat=None, detected_at="2026-09-01T00:00:00Z", target_head="newer",
    )
    assert (await expire(store))["status"] == "superseded"
    assert await leaf4(store) == ("rejected", "superseded", "b_none", "l_off")


# ---------------------------------------------------------------- 종결을 받았다 (wire-v1 3절)


async def test_a_protocol_one_bundle_is_answered_only_when_the_evidence_settles(store: EventStore):
    await seed(store)
    harness = p1(Harness(store))
    try:
        await begin(store)
        await harness.coordinator.handle_result(result("a1"))
        assert sent_of(harness, DeployTerminalReceived) == []  # 반쪽 묶음
        await harness.coordinator.handle_settled(settled("a1"))
        answers = sent_of(harness, DeployTerminalReceived)
        assert [(a.orchestrator_attempt_id, a.outcome, a.deploy_id) for a in answers] == [("a1", "closed_now", DEPLOY_ID)]
        # 같은 묶음이 다시 오면 같은 답을 다시 보낸다
        await harness.coordinator.handle_result(result("a1"))
        await harness.coordinator.handle_settled(settled("a1"))
        assert [a.outcome for a in sent_of(harness, DeployTerminalReceived)] == ["closed_now", "already_closed", "already_closed"]
        assert sent_of(harness, DeployReportAck) == []
    finally:
        await harness.close()


async def test_a_failure_bundle_and_a_late_bundle_are_answered(store: EventStore):
    await seed(store)
    harness = p1(Harness(store))
    try:
        await begin(store, "a1")
        await harness.coordinator.handle_result(result("a1", status="failed"))
        assert [a.outcome for a in sent_of(harness, DeployTerminalReceived)] == ["closed_now"]
        assert await leaf4(store) == ("pending", "retry", "b_none", "l_on")
        # 닫힌 시도의 늦은 묶음: 증거가 종결되면 답한다. 요청을 닫지 않았으면 already_closed
        await begin_via_probe(store, "a2")
        await harness.coordinator.handle_result(result("a1", status="failed"))
        assert [a.outcome for a in sent_of(harness, DeployTerminalReceived)] == ["closed_now", "already_closed"]
    finally:
        await harness.close()


async def test_a_late_success_that_closes_the_request_is_answered_as_closed_now(store: EventStore):
    await seed(store)
    harness = p1(Harness(store))
    try:
        await begin(store)
        await expire(store)  # 짧은 마감으로 닫힘(다시 대기)
        await harness.coordinator.handle_result(result("a1"))
        await harness.coordinator.handle_settled(settled("a1"))
        assert [a.outcome for a in sent_of(harness, DeployTerminalReceived)] == ["closed_now"]
        assert (await leaf4(store))[:2] == ("success", "success")
    finally:
        await harness.close()


async def test_late_success_supersedes_an_unstarted_attempt_before_its_decline(store: EventStore):
    await seed(store)
    harness = p1(Harness(store))
    try:
        await begin(store, "a1")
        await expire(store, "a1")
        await begin_via_probe(store, "a2")
        harness.coordinator._schedule_attempt("a2", future_deadline())
        assert "attempt:a2" in harness.coordinator._timers

        await harness.coordinator.handle_result(result("a1"))
        assert await leaf4(store) == ("deploying", "deploying", "b_unknown", "l_off")
        await harness.coordinator.handle_settled(settled("a1"))

        assert await leaf4(store) == ("success", "success", "b_none", "l_off")
        row = (await raw(store, "SELECT outcome, sm_evidence, execution_started_at FROM deploy_attempts WHERE orchestrator_attempt_id = ?", "a2"))[0]
        assert row == ("superseded", "ev_superseded", None)
        assert "attempt:a2" not in harness.coordinator._timers
        assert [answer.outcome for answer in sent_of(harness, DeployTerminalReceived)] == ["closed_now"]
        assert (await store.attempts.record_node_terminal(terminal("a2")))["status"] == "ignored"
        assert await leaf4(store) == ("success", "success", "b_none", "l_off")
    finally:
        await harness.close()


async def test_late_success_closes_after_the_unstarted_later_attempt_declines(store: EventStore):
    await seed(store)
    await begin(store, "a1")
    await expire(store, "a1")
    await begin_via_probe(store, "a2")
    assert (await store.attempts.record_node_terminal(terminal("a2")))["event"] == "declined"
    assert await leaf4(store) == ("pending", "again", "b_none", "l_off")
    assert (await raw(store, "SELECT execution_started_at FROM deploy_attempts WHERE orchestrator_attempt_id = ?", "a2"))[0] == (None,)

    await store.attempts.record_result(
        deploy_id=DEPLOY_ID, orchestrator_attempt_id="a1", connection_generation="g1", status="success", error=None, duration_ms=5,
    )
    late = await store.attempts.record_settled(
        deploy_id=DEPLOY_ID, orchestrator_attempt_id="a1", connection_generation="g1", local_head="target", remote_head="target",
    )
    assert late["status"] == "success"
    assert await leaf4(store) == ("success", "success", "b_none", "l_off")


async def test_late_success_is_evidence_only_after_a_later_attempt_starts(store: EventStore):
    await seed(store)
    await begin(store, "a1")
    await expire(store, "a1")
    await begin_via_probe(store, "a2")
    await progress(store, "a2")
    assert (await raw(store, "SELECT execution_started_at FROM deploy_attempts WHERE orchestrator_attempt_id = ?", "a2"))[0][0] is not None

    await store.attempts.record_result(
        deploy_id=DEPLOY_ID, orchestrator_attempt_id="a1", connection_generation="g1", status="success", error=None, duration_ms=5,
    )
    late = await store.attempts.record_settled(
        deploy_id=DEPLOY_ID, orchestrator_attempt_id="a1", connection_generation="g1", local_head="target", remote_head="target",
    )
    assert late["status"] == "late_evidence" and late["request_changed"] is False
    assert await leaf4(store) == ("deploying", "deploying", "b_running", "l_off")
    assert (await raw(store, "SELECT sm_evidence, late_evidence FROM deploy_attempts WHERE orchestrator_attempt_id = ?", "a1"))[0] == ("ev_both", "success")


async def test_a_late_recovery_evidence_is_recorded_and_answered(store: EventStore):
    """2b-1이 미룬 닫힌 시도의 늦은 증거 회수: 늦은 결과와 같은 규칙으로 증거에 적고(성공이면 success_evidence) 답한다."""
    await store.create_deploy_event(
        deploy_id=DEPLOY_ID, node_id="n", repo="r", branch="main", commits=["c"], affected_services=[], diff_stat=None,
        detected_at="2026-08-01T00:00:00Z", target_head="target", deployment_kind="manifest",
        expected_manifest_identity="m", expected_manifest_digest="d",
    )
    harness = p1(Harness(store))
    try:
        await begin(store, "a1")
        await expire(store, "a1")  # 짧은 마감: 실행 여부 불명으로 닫히고 source는 a1(실행했을 수 있다)
        probe = await store.attempts.create_probe(
            probe_id="p", deploy_id=DEPLOY_ID, connection_generation="g1", deadline_at=future_deadline(), manual_retry=False,
            requested_orchestrator_attempt_id="a2",
        )
        assert probe["todo"] == ["send_probe"]
        proposal = DeployPlanProposal(
            mode="evidence_recovery", probe_id="p", connection_generation="g1", deploy_id=DEPLOY_ID, node_id="n", repo="r",
            branch="main", target_head="target", current_head="target", journal_status="success", journal_attempt_id="j1",
            linked_orchestrator_attempt_id="a1", journal_target_head="target", manifest_identity="m", manifest_digest="d",
            journal_completed_at="2026-01-01T00:00:00Z", reason="durable_local_success", fingerprint="fp",
        )
        begun = await store.attempts.record_proposal_and_begin(
            proposal, orchestrator_attempt_id="a2", source="auto", approved_by="system:auto", deadline_at=future_deadline(),
            current_generation="g1", node_protocol=1,
        )
        assert begun["status"] == "begun" and begun["todo"] == ["send_permit"]
        await expire(store, "a2")  # 증거 회수 시도도 짧은 마감으로 닫힘(다시 대기)
        assert (await leaf4(store))[:2] == ("pending", "again")
        evidence = ManifestRecoveryEvidence(
            deploy_id=DEPLOY_ID, orchestrator_attempt_id="a2", source_orchestrator_attempt_id="a1", journal_attempt_id="j1",
            connection_generation="g1", node_id="n", repo="r", branch="main", target_head="target", current_head="target",
            manifest_identity="m", manifest_digest="d", journal_status="success", journal_completed_at="2026-01-01T00:00:00Z",
        )
        await harness.coordinator.handle_evidence(evidence)
        # 늦은 증거 회수가 검증을 통과해 성공으로 종결됐고, 뒤 시도가 없어 요청을 닫았다
        assert [a.outcome for a in sent_of(harness, DeployTerminalReceived)] == ["closed_now"]
        assert (await raw(store, "SELECT sm_evidence, late_evidence FROM deploy_attempts WHERE orchestrator_attempt_id = 'a2'"))[0] == ("ev_both", "success")
        assert (await leaf4(store))[:2] == ("success", "success")
        await harness.coordinator.handle_evidence(evidence)
        assert [a.outcome for a in sent_of(harness, DeployTerminalReceived)] == ["closed_now", "already_closed"]
    finally:
        await harness.close()


async def test_a_declined_terminal_is_never_answered_and_legacy_nodes_keep_the_ignored_ack(store: EventStore):
    await seed(store)
    harness = p1(Harness(store))
    try:
        await begin(store)
        await harness.coordinator.handle_terminal(terminal("a1"))
        assert sent_of(harness, DeployTerminalReceived) == []
    finally:
        await harness.close()
    legacy = EventStore(":memory:")
    await legacy.initialize()
    legacy_harness = Harness(legacy)
    try:
        await seed(legacy)
        await begin(legacy, protocol=None)
        await legacy_harness.coordinator.handle_result(result("a1"))
        await legacy_harness.coordinator.handle_settled(settled("a1"))
        await legacy_harness.coordinator.handle_result(result("a1"))  # 종결된 뒤 다시 옴
        assert sent_of(legacy_harness, DeployTerminalReceived) == []
        acks = sent_of(legacy_harness, DeployReportAck)
        assert [(a.status, a.reason) for a in acks] == [("ignored", "terminal_attempt")]
    finally:
        await legacy_harness.close()
        await legacy.close()


# ---------------------------------------------------------------- 요청 줄이 없을 때 온 스스로 한 배포의 실패 (G12)


async def test_a_self_deploy_failure_before_the_request_row_starts_the_later_request_locked(store: EventStore):
    recorded = await store.record_node_deploy_report(node_report("failed", node_attempt_id="na-early"))
    assert (recorded["status"], recorded["closed_now"]) == ("ignored", True)
    assert await raw(store, "SELECT outcome FROM node_deploy_attempts WHERE node_attempt_id = 'na-early'") == [("failed",)]
    # 같은 글이 또 오면 이미 적은 노드 시도라 already_closed
    assert (await store.record_node_deploy_report(node_report("failed", node_attempt_id="na-early")))["closed_now"] is False
    await seed(store)
    assert await leaf4(store) == ("pending", "retry", "b_none", "l_on")
    assert await marker_of(store) == (None, None, 1)
    pending = await store.get_active_deploys()
    assert pending[0]["inspection_required"] is True and pending[0]["last_failure"]["attempt_id"] == "na-early"
    history = await store.get_deploy_history()
    assert any(item["deploy_id"] == "node-attempt:na-early" for item in history)


# ---------------------------------------------------------------- 계획 답의 실패 저널 (10항)


def proposal(probe_id: str, journal_status: str | None, linked: str | None, fingerprint: str = "fp") -> DeployPlanProposal:
    return DeployPlanProposal(
        mode="execute", probe_id=probe_id, connection_generation="g1", deploy_id=DEPLOY_ID, node_id="n", repo="r",
        branch="main", target_head="target", current_head="old", journal_status=journal_status, journal_attempt_id="j1",
        linked_orchestrator_attempt_id=linked, journal_target_head="target", original_previous_head="old",
        manifest_identity=None, manifest_digest=None, journal_completed_at="2026-01-01T00:00:00Z", reason="manifest_retry",
        fingerprint=fingerprint,
    )


async def test_an_auto_plan_carrying_a_failed_journal_of_a_no_closure_locks_without_a_permit(store: EventStore):
    await seed(store)
    await begin(store, "a1")
    await store.attempts.record_node_terminal(terminal("a1"))  # 「아니오」 닫힘(실행하지 않음)으로 다시 대기
    assert await leaf4(store) == ("pending", "again", "b_none", "l_off")
    harness = p1(Harness(store))
    try:
        await harness.coordinator.handle_auto_request(auto_request("a2"))
        probe_msg = await wait_for_send(harness)
        assert isinstance(probe_msg, DeployPlanProbe) and probe_msg.retry_lineage == ["a1"]
        await harness.handle_proposal(proposal(probe_msg.probe_id, "failed", "a1"))
        rejection = harness.sent[-1][1]
        assert isinstance(rejection, RejectedDeployAttemptAck) and rejection.error == "retry_requires_manual_approval"
        assert await leaf4(store) == ("pending", "retry", "b_none", "l_on")
        assert await marker_of(store) == ("a1", "a1", 1)
        assert await store.attempts.get_active_attempts() == []
        probe = await store.attempts.get_probe(probe_msg.probe_id)
        assert (probe["status"], probe["terminal_reason"]) == ("terminal", "retry_requires_manual_approval")
    finally:
        await harness.close()


async def test_a_failed_journal_of_a_yes_closure_or_outside_the_lineage_does_not_trigger_the_rule(store: EventStore):
    await seed(store)
    await begin(store, "a1")
    await store.attempts.fail_attempt_not_sent("a1")  # 「아니오」
    harness = p1(Harness(store))
    try:
        await harness.coordinator.handle_auto_request(auto_request("a2"))
        probe_msg = await wait_for_send(harness)
        # 계보 밖 시도의 저널: 규칙은 쉬고 지금의 재확인이 판정한다(legacy 저장소라 실행 계획을 받는다)
        await harness.handle_proposal(proposal(probe_msg.probe_id, "failed", "zz"))
        assert isinstance(harness.sent[-1][1], AcceptedDeployAttemptAck)
        assert await leaf4(store) == ("deploying", "deploying", "b_unknown", "l_off")
    finally:
        await harness.close()


# ---------------------------------------------------------------- 계보: 「안에 있는가」 (9항)


def probe_row(source: str | None, lineage: list[str]) -> dict:
    return {
        "source_orchestrator_attempt_id": source, "retry_lineage_json": json.dumps(lineage), "target_head": "target",
        "expected_manifest_identity": "m", "expected_manifest_digest": "d",
    }


def test_execute_and_recovery_proposals_accept_any_lineage_attempt_for_protocol_one_requests():
    from haniel_orch.deploy_attempt_validation import DeployAttemptValidation as V

    recovery = DeployPlanProposal(
        mode="evidence_recovery", probe_id="p", connection_generation="g1", deploy_id=DEPLOY_ID, node_id="n", repo="r",
        branch="main", target_head="target", current_head="target", journal_status="success", journal_attempt_id="j",
        linked_orchestrator_attempt_id="a1", journal_target_head="target", manifest_identity="m", manifest_digest="d",
        journal_completed_at="t", reason="durable_local_success", fingerprint="fp",
    )
    execute = recovery.model_copy(update={"mode": "execute", "journal_status": "failed", "original_previous_head": "old", "current_head": "target"})
    probe = probe_row("a2", ["a1", "a2"])
    assert V._validate_recovery_proposal(probe, recovery, legacy=False) is None
    assert "linked source" in (V._validate_recovery_proposal(probe, recovery, legacy=True) or "")
    assert V._validate_execute_proposal(probe, execute, legacy=False) is None
    assert "linked source" in (V._validate_execute_proposal(probe, execute, legacy=True) or "")
    # 계보 밖 시도의 저널은 판 1에서도 닫힌 답이다
    assert "linked source" in (V._validate_recovery_proposal(probe_row("a2", ["a2"]), recovery, legacy=False) or "")


async def test_the_probe_recheck_sees_lineage_containment_for_protocol_one_requests(store: EventStore):
    await seed(store)
    await begin(store, "a1")
    await store.attempts.fail_attempt_not_sent("a1")
    probe = await store.attempts.create_probe(
        probe_id="p", deploy_id=DEPLOY_ID, connection_generation="g1", deadline_at=future_deadline(), manual_retry=False,
        requested_orchestrator_attempt_id="a2",
    )
    # 탐침을 만든 뒤 늦은 실패가 계보를 넓혔다고 치자(a1은 그대로 안에 있다)
    await raw(store, "INSERT INTO deploy_retry_source_attempts VALUES (?, 'zz', ?)", DEPLOY_ID, "2026-01-01T00:00:00Z")
    stale = await store.attempts._proposal_stale_reason(probe, proposal("p", None, None), "auto")
    assert stale is None
    # 판 없는 요청은 「같은가」라 계보가 달라지면 막는다
    await raw(store, "UPDATE deploy_attempts SET node_protocol = NULL")
    stale = await store.attempts._proposal_stale_reason(probe, proposal("p", None, None), "auto")
    assert stale is not None and stale[0] == "retry_lineage_changed"


# ---------------------------------------------------------------- 재기동 복원 (wire-v1 5절)


async def test_restart_restores_deadlines_by_belief(store: EventStore):
    await seed(store)
    for attempt_id, repo in (("q", "r"),):
        pass
    await begin(store, "u")  # 닿았는지 모름
    await raw(store, "UPDATE deploy_attempts SET deadline_at = ? WHERE orchestrator_attempt_id = 'u'", PAST)
    restored = {row["orchestrator_attempt_id"]: row for row in await store.attempts.restore_belief_deadlines(grace_sec=300)}
    assert restored["u"]["deadline_kind"] == DEADLINE_ACK
    assert datetime.fromisoformat(restored["u"]["deadline_at"]) > datetime.now(timezone.utc) + timedelta(seconds=25)
    await received(store, "u")
    restored = {row["orchestrator_attempt_id"]: row for row in await store.attempts.restore_belief_deadlines(grace_sec=300)}
    assert restored["u"]["deadline_kind"] == DEADLINE_GRACE
    assert datetime.fromisoformat(restored["u"]["deadline_at"]) > datetime.now(timezone.utc) + timedelta(seconds=290)
    await progress(store, "u")
    before = (await store.attempts.get_attempt("u"))["deadline_at"]
    restored = {row["orchestrator_attempt_id"]: row for row in await store.attempts.restore_belief_deadlines(grace_sec=300)}
    assert (restored["u"]["deadline_kind"], restored["u"]["deadline_at"]) == (DEADLINE_LEASE, before)


async def test_legacy_restart_keeps_the_stored_lease(store: EventStore):
    await seed(store)
    await begin(store, "l", protocol=None)
    before = await store.attempts.get_attempt("l")
    restored = await store.attempts.restore_belief_deadlines(grace_sec=300)
    assert (restored[0]["deadline_kind"], restored[0]["deadline_at"]) == (DEADLINE_LEASE, before["deadline_at"])
    harness = Harness(store)
    try:
        await harness.coordinator.restore_deadlines()
        assert "attempt:l" in harness.coordinator._timers
    finally:
        await harness.close()


# ---------------------------------------------------------------- 옛 노드 그대로


async def test_legacy_attempts_never_see_again_received_hello_or_grace(store: EventStore):
    await seed(store)
    harness = Harness(store)  # 판 없음
    try:
        await begin(store, "l1", protocol=None)
        assert (await received(store, "l1"))["status"] == "ignored"
        assert await store.attempts.mark_link_down("n", grace_sec=300) == []
        assert await store.attempts.reconcile_hello("n", {"other"}) == []
        assert (await attempt_cols(store, "l1"))[1] == DEADLINE_LEASE
        closed = await store.attempts.record_node_terminal(terminal("l1", kind="revalidation"))
        assert closed["event"] == "attempt_failed"
        assert await leaf4(store) == ("pending", "retry", "b_none", "l_on")
        await begin_via_probe(store, "l2", protocol=None)
        assert (await expire(store, "l2"))["event"] == "attempt_failed"
        assert await leaf4(store) == ("pending", "retry", "b_none", "l_on")
        assert "again" not in {row[0] for row in await raw(store, "SELECT sm_status FROM deploy_events")}
    finally:
        await harness.close()


# ---------------------------------------------------------------- 받기 함수와 코디네이터의 일치 (13항)


async def _row(store: EventStore) -> dict:
    status, leaf, belief, lock = await leaf4(store)
    tag = (await raw(store, "SELECT sm_tag FROM deploy_events WHERE deploy_id = ?", DEPLOY_ID))[0][0]
    return {"status": leaf, "belief": belief, "lock": lock, "tag": tag}


@pytest.mark.parametrize(
    "kind",
    ["received", "progress", "terminal_declined", "terminal_running", "send_failed", "ack_deadline", "grace_deadline", "lease_deadline", "hello_holds", "hello_missing", "link_down"],
)
async def test_the_coordinator_lands_where_the_intake_and_one_definition_step_say(store: EventStore, kind):
    """글 종류마다 가짜 노드로 코디네이터를 돌린 결과 줄이, 받기 함수가 낸 (사건, 사실)을 restore.advance로 한 걸음 넣은 결과와 같다."""
    await seed(store)
    harness = p1(Harness(store))
    try:
        await begin(store, "a1")
        if kind in ("grace_deadline", "hello_holds"):
            await received(store)
        if kind in ("terminal_running", "lease_deadline"):
            await progress(store)
        before = await _row(store)
        attempt = await store.attempts.get_attempt("a1")
        held = None
        if kind == "received":
            intake = intake_received(node_protocol=attempt["node_protocol"], active=True)
            await harness.coordinator.handle_received(DeployAttemptReceived(node_id="n", deploy_id=DEPLOY_ID, orchestrator_attempt_id="a1", connection_generation="g1"))
        elif kind == "progress":
            intake = intake_progress(active=True)
            await harness.coordinator.handle_progress(DeployProgress(deploy_id=DEPLOY_ID, node_id="n", orchestrator_attempt_id="a1", connection_generation="g1", stage="build"))
        elif kind in ("terminal_declined", "terminal_running"):
            intake = intake_terminal(node_protocol=attempt["node_protocol"], belief=before["belief"], newer=False)
            await harness.coordinator.handle_terminal(terminal("a1"))
        elif kind == "send_failed":
            intake = intake_send_failed(node_protocol=attempt["node_protocol"], newer=False)
            await harness.coordinator._close_unsent_attempt("n", "a1")
        elif kind in ("ack_deadline", "lease_deadline", "grace_deadline"):
            if kind == "grace_deadline":
                await store.attempts.mark_link_down("n", grace_sec=300)
            deadline_kind = (await store.attempts.get_attempt("a1"))["deadline_kind"]
            intake = intake_deadline(node_protocol=attempt["node_protocol"], deadline_kind=deadline_kind, belief=before["belief"], newer=False)
            await raw(store, "UPDATE deploy_attempts SET deadline_at = ? WHERE orchestrator_attempt_id = 'a1'", PAST)
            await harness.coordinator._attempt_timeout("a1", 0)
        elif kind in ("hello_holds", "hello_missing"):
            held = {"a1"} if kind == "hello_holds" else set()
            intake = intake_hello(node_protocol=attempt["node_protocol"], belief=before["belief"], held=held, attempt_id="a1", newer=False)
            await harness.coordinator.reconcile_hello("n", 1, [HeldAttempt(orchestrator_attempt_id=a, state="queued") for a in held])
        else:
            from haniel_orch.deploy_attempt_intake import intake_link_down

            intake = intake_link_down()
            await store.attempts.mark_link_down("n", grace_sec=300)
        assert intake is not None, kind
        expected = advance(OrchRequest, before, intake.event, intake.facts)
        assert await _row(store) == expected, kind
    finally:
        await harness.close()


def test_warnings_for_expected_no_transitions_are_debug(caplog):
    from haniel_orch.statechart.restore import NoTransition, QUIET_NO_TRANSITION

    assert QUIET_NO_TRANSITION == {"observed", "reopen"}
    with caplog.at_level(logging.DEBUG, logger="haniel_orch.statechart"):
        NoTransition("OrchRequest", {"status": "retry"}, "observed").warn("observed")
        NoTransition("OrchRequest", {"status": "pending"}, "reopen").warn("reopen")
        NoTransition("OrchRequest", {"status": "pending"}, "acked").warn("acked")
    levels = {record.getMessage().split("사건=")[1].split(" ")[0]: record.levelno for record in caplog.records if "전이 없음" in record.getMessage()}
    assert levels == {"observed": logging.DEBUG, "reopen": logging.DEBUG, "acked": logging.WARNING}
