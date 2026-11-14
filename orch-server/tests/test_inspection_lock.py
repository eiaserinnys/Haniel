"""점검 필요(lock) 잠금 시험(카드 #1259 PR 2a).

실제로 돌다 실패한 커밋(시도 실패, 실행 임대 마감, 노드가 스스로 한 배포의 실패)과 보내기 실패는 점검 필요를 켠다.
점검 필요는 원천이 단건 승인(manual_single)인 승인이 시도를 열 때에만 내려간다. 자동 요청, 일괄 승인, 되살림은
점검 필요가 켜진 요청의 시도를 열지 못한다.

이 파일의 `test_*` 가운데 이름이 `test_repro_`로 시작하는 것은 PR 1 판(바꾸기 전 커밋)에서 실패해야 하는 재현 시험이고,
`test_negative_control_`로 시작하는 것은 장치 하나를 끄면 실패해야 하는 음성 대조 시험이다.
"""

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from state_support import fire, to_deploying
from test_deploy_attempt_coordinator import Harness, auto_request, make_retry, seed
from haniel_orch.api import create_api_routes
from haniel_orch.deploy_attempt_coordinator import PlanRejected
from haniel_orch.deploy_attempt_store import DeployAttemptStore
from haniel_orch.event_store import EventStore
from haniel_orch.hub import WebSocketHub
from haniel_orch.node_registry import NodeRegistry
from haniel_orch.protocol import (
    AcceptedDeployAttemptAck,
    DeployAttemptTerminal,
    DeployPlanProbe,
    DeployPlanProposal,
    DeployApproval,
    NodeDeployReport,
    NodeHello,
    RejectedDeployAttemptAck,
)
from haniel_orch.statechart import steps
from haniel_orch.statechart.definitions import OrchRequest
from haniel_orch.statechart.restore import NoTransition, advance

DEPLOY_ID = "n:r:main:target"


# ---------------------------------------------------------------- 도우미


def node_report(
    phase: str = "failed",
    node_attempt_id: str = "na1",
    deploy_id: str = DEPLOY_ID,
    branch: str = "main",
    node_id: str = "n",
    repo: str = "r",
) -> NodeDeployReport:
    target = deploy_id.rsplit(":", 1)[-1]
    extra = (
        {"error": "boom", "local_head": "old"}
        if phase == "failed"
        else {"local_head": target}
    )
    return NodeDeployReport(
        phase=phase,
        node_attempt_id=node_attempt_id,
        deploy_id=deploy_id,
        node_id=node_id,
        repo=repo,
        branch=branch,
        target_head=target,
        trigger="local",
        duration_ms=None if phase == "started" else 5,
        **extra,
    )


async def raw(store: EventStore, sql: str, *params):
    cursor = await store._db.execute(sql, params)
    rows = await cursor.fetchall()
    if sql.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")):
        await store._db.commit()
    return rows


# PR 1 판(바꾸기 전 커밋)에는 점검 필요 칸이 없다. 칸이 없으면 None으로 읽어, 재현 시험이 「칸이 없다」는 오류가 아니라
# 기대한 값과 다르다는 단언으로 실패하게 한다.
async def lock_of(store: EventStore, deploy_id: str = DEPLOY_ID):
    try:
        return (await raw(store, "SELECT sm_lock FROM deploy_events WHERE deploy_id = ?", deploy_id))[0][0]
    except sqlite3.OperationalError:
        return None


async def leaf_of(store: EventStore, deploy_id: str = DEPLOY_ID) -> tuple:
    status, leaf = (
        await raw(store, "SELECT status, sm_status FROM deploy_events WHERE deploy_id = ?", deploy_id)
    )[0]
    return (status, leaf, await lock_of(store, deploy_id))


async def marker_of(store: EventStore, deploy_id: str = DEPLOY_ID):
    try:
        rows = await raw(
            store,
            "SELECT source_orchestrator_attempt_id, last_failed_orchestrator_attempt_id, inspection_required "
            "FROM deploy_retry_requirements WHERE deploy_id = ?",
            deploy_id,
        )
    except sqlite3.OperationalError:
        rows = await raw(
            store,
            "SELECT source_orchestrator_attempt_id, last_failed_orchestrator_attempt_id, NULL "
            "FROM deploy_retry_requirements WHERE deploy_id = ?",
            deploy_id,
        )
    return rows[0] if rows else None


async def lineage_of(store: EventStore, deploy_id: str = DEPLOY_ID) -> list[str]:
    rows = await raw(
        store,
        "SELECT lineage_orchestrator_attempt_id FROM deploy_retry_source_attempts WHERE deploy_id = ? ORDER BY 1",
        deploy_id,
    )
    return [row[0] for row in rows]


def future_deadline() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()


async def begin_and_fail(store: EventStore, attempt_id: str = "failed-source") -> None:
    """단건 승인으로 시도를 열고 실패시킨다. 요청은 점검 필요가 켜진 표시 줄 있는 대기가 된다."""
    await store.attempts.begin_normal_attempt(
        orchestrator_attempt_id=attempt_id,
        deploy_id=DEPLOY_ID,
        connection_generation="g1",
        current_generation="g1",
        source="manual_single",
        approved_by="director",
        deadline_at=future_deadline(),
    )
    await store.attempts.fail_active_attempt(
        attempt_id, kind="deploy_result_failed", stage="execution", reason="boom", error="e"
    )


def proposal_for(probe: DeployPlanProbe, harness: Harness, fingerprint: str = "fp") -> DeployPlanProposal:
    return DeployPlanProposal(
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
        fingerprint=fingerprint,
    )


async def wait_sent(harness: Harness, count: int) -> None:
    for _ in range(200):
        if len(harness.sent) >= count:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("coordinator did not send a message")


# ---------------------------------------------------------------- 정의의 한 걸음


@pytest.mark.parametrize(
    ("row", "event", "facts", "expected"),
    [
        # 점검 필요가 꺼져 있으면 어느 원천이든 연다. 켤 일은 없다
        (("pending", "l_off"), "begin_attempt", {"manual_single": False}, ("deploying", "l_off")),
        (("pending", "l_off"), "begin_attempt", {"manual_single": True}, ("deploying", "l_off")),
        # 점검 필요가 켜져 있으면 단건 승인만 열고, 그때 내린다
        (("retry", "l_on"), "begin_attempt", {"manual_single": True}, ("deploying", "l_off")),
        (("retry", "l_on"), "begin_attempt", {"manual_single": False}, None),
        (("pending", "l_on"), "begin_attempt", {"manual_single": False}, None),
        # 시도의 실패와 보내기 실패는 모두 켠다. 닫힌 요청에는 점검 필요만 켠다
        (("deploying", "l_off"), "attempt_failed", {"newer": False}, ("retry", "l_on")),
        (("deploying", "l_off"), "send_failed", {"newer": False}, ("retry", "l_on")),
        (("deploying", "l_off"), "attempt_failed", {"newer": True}, ("superseded", "l_on")),
        (("rejected", "l_off"), "attempt_failed", {"newer": False}, ("rejected", "l_on")),
        (("success", "l_off"), "attempt_failed", {"newer": True}, ("success", "l_on")),
        (("rejected", "l_on"), "attempt_failed", {"newer": False}, None),
        # 노드가 스스로 한 배포의 실패
        (("pending", "l_off"), "node_failed", {"no_active_attempt": True}, ("retry", "l_on")),
        (("retry", "l_off"), "node_failed", {"no_active_attempt": True}, ("retry", "l_on")),
        (("retry", "l_on"), "node_failed", {"no_active_attempt": True}, None),
        (("deploying", "l_off"), "node_failed", {"no_active_attempt": False}, ("deploying", "l_on")),
        (("deploying", "l_off"), "node_failed", {"no_active_attempt": True}, ("retry", "l_on")),
        (("success", "l_off"), "node_failed", {"no_active_attempt": True}, None),
        # 닫힘(성공, 거절, 밀림)은 점검 필요를 건드리지 않는다
        (("deploying", "l_on"), "attempt_succeeded", {}, ("success", "l_on")),
        (("retry", "l_on"), "reject", {}, ("rejected", "l_on")),
        (("retry", "l_on"), "newer_commit", {}, ("superseded", "l_on")),
        (("retry", "l_on"), "node_succeeded", {}, ("success", "l_on")),
        # 거절은 처음 대기와 표시 줄 있는 대기에서만 된다
        (("deploying", "l_off"), "reject", {}, None),
        (("success", "l_off"), "reject", {}, None),
        (("rejected", "l_off"), "reject", {}, None),
        (("superseded", "l_off"), "reject", {}, None),
    ],
)
def test_lock_definition_matrix(row, event, facts, expected):
    before = {"status": row[0], "lock": row[1], "tag": "t_off"}  # 꼬리표 영역(2b-1)은 이 사건들이 건드리지 않는다
    if expected is None:
        with pytest.raises(NoTransition):
            advance(OrchRequest, before, event, facts)
    else:
        assert advance(OrchRequest, before, event, facts) == {
            "status": expected[0],
            "lock": expected[1],
            "tag": "t_off",
        }


@pytest.mark.parametrize(
    ("lock", "expected"),
    [("l_on", ("retry", "l_on")), ("l_off", ("pending", "l_off"))],
)
def test_reopen_follows_the_lock(lock, expected):
    facts = {"self_update": True, "postponed_due": True, "no_other_open": True}
    for closed in ("rejected", "superseded"):
        after = advance(OrchRequest, {"status": closed, "lock": lock, "tag": "t_off"}, "reopen", facts)
        assert after == {"status": expected[0], "lock": expected[1], "tag": "t_off"}


# ---------------------------------------------------------------- PR 1 검수에서 남긴 두 갈래


async def test_repro_a_closed_superseded_row_gets_only_the_lock_when_its_attempt_fails(store: EventStore):
    """PR 1 판은 더 새 커밋이 없어도 밀림 줄의 사유와 updated_at을 덮었다. 이 판은 점검 필요만 켠다."""
    await seed(store)
    await fire(store, DEPLOY_ID, "newer_commit", data={"reason": "superseded by newer-one"})
    before = (await raw(store, "SELECT status, reject_reason, updated_at FROM deploy_events"))[0]

    async with store._mutation_lock:
        await steps.step_request(
            store._db,
            DEPLOY_ID,
            "attempt_failed",
            facts={"newer": True},
            data={"failed_attempt_id": "a1", "stable_source": "a1", "marker_created_at": None},
        )
        await store._db.commit()

    after = (await raw(store, "SELECT status, reject_reason, updated_at FROM deploy_events"))[0]
    assert after == before == ("rejected", "superseded by newer-one", before[2])
    assert await lock_of(store) == "l_on"
    assert await marker_of(store) is None and await lineage_of(store) == []


async def test_repro_a_rejected_self_update_revived_while_locked_is_a_retry_that_observation_cannot_close(
    store: EventStore,
):
    """PR 1 판은 표시 줄이 있는 거절 줄을 되살리면 처음 대기 잎이 되어 관측이 닫을 수 있었다."""
    await store.create_deploy_event(
        deploy_id=DEPLOY_ID,
        node_id="n",
        repo="haniel",
        branch="main",
        commits=["c"],
        affected_services=[],
        diff_stat=None,
        detected_at="2026-08-01T00:00:00Z",
        target_head="target",
        is_self_update=True,
    )
    await begin_and_fail(store)  # 점검 필요가 켜지고 표시 줄이 선다
    assert await lock_of(store) == "l_on"
    await store.reject_deploy(DEPLOY_ID, "postpone")  # 닫히면 표시 줄은 지워지지만 점검 필요는 남는다
    await store.set_deploy_postponed_until(DEPLOY_ID, "2000-01-01T00:00:00+00:00")
    assert await store.attempts.has_retry_requirement(DEPLOY_ID) is False
    assert await lock_of(store) == "l_on"

    assert await store.reopen_postponed_self_update(DEPLOY_ID) is True

    assert await leaf_of(store) == ("pending", "retry", "l_on")
    assert await marker_of(store) == (None, None, 1)  # 두 시도 번호 칸이 빈 표시 줄
    async with store._mutation_lock:
        with pytest.raises(NoTransition):
            await steps.step_request(
                store._db,
                DEPLOY_ID,
                "observed",
                facts={"heads_equal": True, "no_active_attempt": True, "no_failed_node_attempt": True},
            )


async def test_a_reopened_self_update_without_the_lock_is_a_plain_pending(store: EventStore):
    await store.create_deploy_event(
        deploy_id=DEPLOY_ID,
        node_id="n",
        repo="haniel",
        branch="main",
        commits=["c"],
        affected_services=[],
        diff_stat=None,
        detected_at="2026-08-01T00:00:00Z",
        target_head="target",
        is_self_update=True,
    )
    await store.reject_deploy(DEPLOY_ID, "postpone")
    await store.set_deploy_postponed_until(DEPLOY_ID, "2000-01-01T00:00:00+00:00")

    assert await store.reopen_postponed_self_update(DEPLOY_ID) is True

    assert await leaf_of(store) == ("pending", "pending", "l_off")
    assert await marker_of(store) is None


# ---------------------------------------------------------------- 어긋남 8


async def test_rest_reject_of_a_deploying_request_is_409_and_the_row_stays(store: EventStore):
    await seed(store)
    await to_deploying(store, DEPLOY_ID)
    hub = WebSocketHub(NodeRegistry(store), store, token="t")
    client = TestClient(Starlette(routes=create_api_routes(hub, store)))

    response = client.post("/api/orch/reject", json={"deploy_id": DEPLOY_ID, "reason": "late"})

    assert response.status_code == 409
    assert response.json() == {"error": "deploy is 'deploying', not pending"}
    event = await store.get_deploy_event(DEPLOY_ID)
    assert event["status"] == "deploying" and event["reject_reason"] is None


async def test_repro_rest_reject_that_races_with_an_approval_gets_the_same_409(store: EventStore, monkeypatch):
    """잠금 밖 상태 확인은 대기로 읽었는데 그 사이 승인이 시도를 열었다. 정의가 거절을 받지 않으면 같은 409다."""
    await seed(store)
    await to_deploying(store, DEPLOY_ID)
    hub = WebSocketHub(NodeRegistry(store), store, token="t")
    sent = []
    hub.send_to_node = AsyncMock(side_effect=lambda *a, **k: sent.append(a))
    real_get = store.get_deploy_event
    calls = {"n": 0}

    async def stale_first_read(deploy_id: str):
        event = await real_get(deploy_id)
        calls["n"] += 1
        if calls["n"] == 1 and event is not None:
            return {**event, "status": "pending"}  # 잠금 밖 확인이 읽은 옛 값
        return event

    monkeypatch.setattr(store, "get_deploy_event", stale_first_read)
    client = TestClient(Starlette(routes=create_api_routes(hub, store)))

    response = client.post("/api/orch/reject", json={"deploy_id": DEPLOY_ID, "reason": "late"})

    assert response.status_code == 409
    assert response.json() == {"error": "deploy is 'deploying', not pending"}
    assert (await real_get(DEPLOY_ID))["status"] == "deploying"
    assert sent == []  # 거절이 노드에 나가지 않는다


# ---------------------------------------------------------------- 어긋남 9: 보내기 실패


class Unsendable:
    """주어진 종류의 글은 보내지 못하는 것으로 만든다(나머지는 보낸 것으로 기록한다)."""

    def __init__(self, harness: Harness, *kinds: type) -> None:
        self.harness = harness
        self.kinds = kinds
        real_send = harness.coordinator._send

        async def send(node_id: str, generation: str, message: object) -> bool:
            if isinstance(message, self.kinds):
                return False
            return await real_send(node_id, generation, message)

        harness.coordinator._send = send


async def assert_unsent_closed(store: EventStore, *, source):
    attempts = await raw(
        store,
        "SELECT orchestrator_attempt_id, outcome, terminal_kind, terminal_reason FROM deploy_attempts "
        "ORDER BY rowid",
    )
    new_attempt = attempts[-1]
    assert new_attempt[1] == "failed"
    assert new_attempt[3] == "approval_send_failed"
    assert await store.attempts.get_active_attempts() == []
    assert await leaf_of(store) == ("pending", "retry", "l_on")
    marker = await marker_of(store)
    assert marker == (source, new_attempt[0], 1)
    assert new_attempt[0] in await lineage_of(store)
    return new_attempt[0]


async def test_repro_unsent_normal_approval_closes_the_attempt_at_once(store: EventStore):
    event = await seed(store)
    harness = Harness(store)
    Unsendable(harness, DeployApproval)
    try:
        with pytest.raises(PlanRejected) as exc:
            await harness.coordinator.approve_manual(event, approved_by="director", source="manual_single")
        assert (exc.value.status_code, str(exc.value)) == (503, "node not connected")

        await assert_unsent_closed(store, source=None)  # 첫 시도라 옮길 source가 없다
        assert harness.coordinator._timers == {}  # 마감 타이머도 걸지 않는다
    finally:
        await harness.close()


async def test_repro_unsent_approval_after_a_probe_keeps_the_source(store: EventStore):
    event = await seed(store)
    harness = Harness(store)
    try:
        await make_retry(store, harness.generation)
        Unsendable(harness, DeployApproval)
        task = asyncio.create_task(
            harness.coordinator.approve_manual(event, approved_by="director", source="manual_single")
        )
        await wait_sent(harness, 1)
        probe = harness.sent[-1][1]
        assert isinstance(probe, DeployPlanProbe)
        await harness.handle_proposal(proposal_for(probe, harness))

        with pytest.raises(PlanRejected) as exc:
            await task
        assert (exc.value.status_code, str(exc.value)) == (503, "approval delivery failed")

        new_id = await assert_unsent_closed(store, source="failed-source")  # source는 옮기지 않는다
        assert await lineage_of(store) == sorted(["failed-source", new_id])
    finally:
        await harness.close()


async def test_repro_unsent_auto_permit_closes_the_attempt(store: EventStore):
    await seed(store)
    harness = Harness(store)
    try:
        await harness.coordinator.handle_auto_request(auto_request("auto-1"))
        probe = harness.sent[-1][1]
        Unsendable(harness, AcceptedDeployAttemptAck)
        await harness.handle_proposal(
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

        new_id = await assert_unsent_closed(store, source=None)
        assert new_id == "auto-1"
    finally:
        await harness.close()


async def test_unsent_approval_of_a_request_with_a_newer_commit_is_superseded(store: EventStore):
    event = await seed(store)
    harness = Harness(store)
    Unsendable(harness, DeployApproval)
    try:
        real_send = harness.coordinator._send

        async def newer_commit_then_fail(node_id, generation, message):
            await store.create_deploy_event(
                deploy_id="n:r:main:newer",
                node_id="n",
                repo="r",
                branch="main",
                commits=["c"],
                affected_services=[],
                diff_stat=None,
                detected_at="2026-09-01T00:00:00Z",
                target_head="newer",
            )
            return await real_send(node_id, generation, message)

        harness.coordinator._send = newer_commit_then_fail
        with pytest.raises(PlanRejected):
            await harness.coordinator.approve_manual(event, approved_by="director", source="manual_single")

        row = (await raw(store, "SELECT status, reject_reason FROM deploy_events WHERE deploy_id = ?", DEPLOY_ID))[0]
        assert row == ("rejected", "superseded by newer canonical")
        assert await lock_of(store) == "l_on"
        assert await marker_of(store) is None  # 밀린 요청의 표시 줄은 닫힘이 지운다
    finally:
        await harness.close()


# ---------------------------------------------------------------- verification_failed 재승인


def _probe_and_proposal(status: str):
    probe = {
        "expected_manifest_identity": "m",
        "expected_manifest_digest": "d",
        "target_head": "t",
        "source_orchestrator_attempt_id": "a1",
        "retry_lineage_json": json.dumps(["a1"]),
    }
    proposal = DeployPlanProposal(
        mode="execute",
        probe_id="p",
        connection_generation="g1",
        deploy_id="x",
        node_id="n",
        repo="r",
        branch="main",
        target_head="t",
        current_head="t",
        reason="manifest_retry",
        fingerprint="fp",
        journal_status=status,
        journal_attempt_id="a1",
        journal_target_head="t",
        original_previous_head="old",
        linked_orchestrator_attempt_id="a1",
        manifest_identity="m",
        manifest_digest="d",
        journal_completed_at="2026-08-01T00:00:00Z",
    )
    return probe, proposal


@pytest.mark.parametrize("status", ["failed", "verification_failed"])
def test_repro_execute_plan_accepts_both_failure_journals(status):
    """건강 검진 실패는 되돌리지 않고 verification_failed로 적힌다. 그 저널로도 다시 승인할 수 있다."""
    probe, proposal = _probe_and_proposal(status)
    assert DeployAttemptStore._validate_execute_proposal(probe, proposal) is None


def test_execute_plan_still_rejects_a_success_journal():
    probe, proposal = _probe_and_proposal("success")
    assert "failed journal" in DeployAttemptStore._validate_execute_proposal(probe, proposal)


# ---------------------------------------------------------------- 노드가 스스로 한 배포의 실패


async def test_repro_node_failure_on_a_first_pending_request_makes_a_blank_marker_and_locks(store: EventStore):
    await seed(store)

    result = await store.record_node_deploy_report(node_report("failed"))

    assert result["status"] == "failed" and result["canonical_status"] == "pending"
    assert await leaf_of(store) == ("pending", "retry", "l_on")
    assert await marker_of(store) == (None, None, 1)
    assert await lineage_of(store) == []


async def test_node_failure_on_a_retry_only_turns_the_lock_on(store: EventStore):
    await seed(store)
    await begin_and_fail(store)
    await raw(store, "UPDATE deploy_events SET sm_lock = 'l_off'")  # 점검 필요만 꺼진 줄(되돌렸다 올린 DB를 흉내 낸다)
    before_marker = await marker_of(store)

    await store.record_node_deploy_report(node_report("failed"))

    assert await leaf_of(store) == ("pending", "retry", "l_on")
    assert (await marker_of(store))[:2] == before_marker[:2]  # source와 마지막 실패 시도는 그대로


async def test_node_failure_during_a_running_attempt_only_turns_the_lock_on(store: EventStore):
    await seed(store)
    await store.attempts.begin_normal_attempt(
        orchestrator_attempt_id="running",
        deploy_id=DEPLOY_ID,
        connection_generation="g1",
        current_generation="g1",
        source="manual_single",
        approved_by="director",
        deadline_at=future_deadline(),
    )
    updated_before = (await store.get_deploy_event(DEPLOY_ID))["updated_at"]

    result = await store.record_node_deploy_report(node_report("failed"))

    assert result["canonical_status"] == "deploying"
    assert await leaf_of(store) == ("deploying", "deploying", "l_on")
    assert [a["orchestrator_attempt_id"] for a in await store.attempts.get_active_attempts()] == ["running"]
    assert (await store.get_deploy_event(DEPLOY_ID))["updated_at"] == updated_before  # 요청 줄의 다른 칸은 그대로
    assert await marker_of(store) == (None, None, 1)  # 표시 줄이 없었으면 빈 칸으로 만든다


async def test_node_failure_while_deploying_without_a_running_attempt_goes_to_retry(store: EventStore):
    await seed(store)
    await to_deploying(store, DEPLOY_ID)

    await store.record_node_deploy_report(node_report("failed"))

    assert await leaf_of(store) == ("pending", "retry", "l_on")


async def test_node_failure_on_a_closed_request_and_a_repeated_report_are_ignored(store: EventStore):
    await seed(store)
    first = await store.record_node_deploy_report(node_report("failed"))
    again = await store.record_node_deploy_report(node_report("failed"))  # 이미 종결된 노드 시도
    assert first["status"] == "failed" and again["status"] == "ignored"
    assert await leaf_of(store) == ("pending", "retry", "l_on")

    await fire(store, DEPLOY_ID, "node_succeeded", data={"duration_ms": 1})
    closed = await store.record_node_deploy_report(node_report("failed", node_attempt_id="na2"))
    assert closed["status"] == "ignored"
    assert (await leaf_of(store))[:2] == ("success", "success")


async def test_repro_auto_request_after_a_node_self_deploy_failure_is_rejected_with_an_ack(store: EventStore):
    await seed(store)
    harness = Harness(store)
    try:
        await store.record_node_deploy_report(node_report("failed"))

        await harness.coordinator.handle_auto_request(auto_request("auto-after-node-failure"))

        ack = harness.sent[-1][1]
        assert isinstance(ack, RejectedDeployAttemptAck)
        assert ack.error == "retry_requires_manual_approval"
        assert await store.attempts.get_active_probes() == []
        assert await store.attempts.get_active_attempts() == []
    finally:
        await harness.close()


# ---------------------------------------------------------------- source 빈 표시 줄의 점검 뒤 승인


async def test_repro_inspected_approval_of_a_blank_marker_takes_the_first_approval_path(store: EventStore):
    event = await seed(store)
    harness = Harness(store)
    try:
        await store.record_node_deploy_report(node_report("failed"))

        attempt_id = await harness.coordinator.approve_manual(event, approved_by="director", source="manual_single")

        permit = harness.sent[-1][1]
        assert isinstance(permit, DeployApproval)  # 계획 묻기 없이 바로 허가가 나간다
        assert permit.orchestrator_attempt_id == attempt_id and permit.execution_mode == "execute"
        assert permit.preflight_fingerprint is None and permit.probe_id is None
        assert not [m for _n, m in harness.sent if isinstance(m, DeployPlanProbe)]
        assert await store.attempts.get_active_probes() == []
        assert await leaf_of(store) == ("deploying", "deploying", "l_off")  # 점검 필요가 내려간다
        assert await marker_of(store) == (None, None, 0)
        active = {d["deploy_id"]: d for d in await store.get_active_deploys()}
        assert active[DEPLOY_ID]["inspection_required"] is False
        assert active[DEPLOY_ID]["last_failure"]["kind"] == "node_deploy"
    finally:
        await harness.close()


async def test_a_blank_marker_is_not_opened_by_a_batch_approval(store: EventStore):
    event = await seed(store)
    harness = Harness(store)
    try:
        await store.record_node_deploy_report(node_report("failed"))

        with pytest.raises(PlanRejected) as exc:
            await harness.coordinator.approve_manual(event, approved_by="dashboard", source="manual_batch")

        assert (exc.value.status_code, str(exc.value)) == (409, "inspection_required")
        assert await store.attempts.get_active_attempts() == []
        assert await lock_of(store) == "l_on"
    finally:
        await harness.close()


# ---------------------------------------------------------------- 탐침 재확인


async def _open_probe(harness: Harness, event: dict, source: str, approved_by: str):
    before = len(harness.sent)
    task = asyncio.create_task(harness.coordinator.approve_manual(event, approved_by=approved_by, source=source))
    await wait_sent(harness, before + 1)
    probe = harness.sent[-1][1]
    assert isinstance(probe, DeployPlanProbe)
    return task, probe


async def test_repro_probe_opened_by_a_batch_approval_cannot_cross_the_lock(store: EventStore):
    """일괄 승인이 점검 필요가 꺼진 동안 연 탐침이 답을 기다리는 사이 노드가 스스로 한 배포가 실패했다."""
    event = await seed(store)
    harness = Harness(store)
    try:
        await make_retry(store, harness.generation)
        await raw(store, "UPDATE deploy_events SET sm_lock = 'l_off'")  # 탐침을 만들 때는 점검 필요가 없었다
        task, probe = await _open_probe(harness, event, "manual_batch", "dashboard")
        await store.record_node_deploy_report(node_report("failed"))  # 그 사이 점검 필요가 선다
        assert await lock_of(store) == "l_on"

        await harness.handle_proposal(proposal_for(probe, harness))

        with pytest.raises(PlanRejected) as exc:
            await task
        assert exc.value.status_code == 422
        assert [a["orchestrator_attempt_id"] for a in await store.attempts.get_active_attempts()] == []
        assert await lock_of(store) == "l_on"
        assert not [m for _n, m in harness.sent if isinstance(m, DeployApproval)]
    finally:
        await harness.close()


async def test_a_single_approval_probe_crosses_the_lock_and_lowers_it(store: EventStore):
    """대조: 같은 경쟁에서 단건 승인이 연 탐침은 시도를 열고 점검 필요를 내린다."""
    event = await seed(store)
    harness = Harness(store)
    try:
        await make_retry(store, harness.generation)
        task, probe = await _open_probe(harness, event, "manual_single", "director")
        await store.record_node_deploy_report(node_report("failed"))

        await harness.handle_proposal(proposal_for(probe, harness))

        attempt_id = await task
        assert isinstance(harness.sent[-1][1], DeployApproval)
        assert [a["orchestrator_attempt_id"] for a in await store.attempts.get_active_attempts()] == [attempt_id]
        assert await leaf_of(store) == ("deploying", "deploying", "l_off")
    finally:
        await harness.close()


async def test_repro_auto_probe_rejected_after_the_lock_rises_gets_the_rejection_ack(store: EventStore):
    await seed(store)
    harness = Harness(store)
    try:
        await harness.coordinator.handle_auto_request(auto_request("auto-x"))
        probe = harness.sent[-1][1]
        assert isinstance(probe, DeployPlanProbe)
        await store.record_node_deploy_report(node_report("failed"))  # 탐침을 만든 뒤 점검 필요가 섰다

        await harness.handle_proposal(
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
                fingerprint="fp-late",
            )
        )

        ack = harness.sent[-1][1]
        assert isinstance(ack, RejectedDeployAttemptAck)
        assert (ack.requested_orchestrator_attempt_id, ack.error) == ("auto-x", "retry_requires_manual_approval")
        assert await store.attempts.get_active_attempts() == []
    finally:
        await harness.close()


async def test_a_probe_whose_lineage_changed_is_rejected(store: EventStore):
    event = await seed(store)
    harness = Harness(store)
    try:
        await make_retry(store, harness.generation)
        task, probe = await _open_probe(harness, event, "manual_single", "director")
        await raw(store, "INSERT INTO deploy_retry_source_attempts VALUES (?, 'extra', 'z')", DEPLOY_ID)

        await harness.handle_proposal(proposal_for(probe, harness))

        with pytest.raises(PlanRejected):
            await task
        assert await store.attempts.get_active_attempts() == []
    finally:
        await harness.close()


# ---------------------------------------------------------------- 일괄 승인


async def _bulk_client(store: EventStore, nodes=("n1",)):
    registry = NodeRegistry(store)
    hub = WebSocketHub(registry, store, token="t")
    for node_id in nodes:
        hello = NodeHello(node_id=node_id, token="t", hostname="h", os="Linux", arch="x86_64", haniel_version="0.1.0")
        await hub.deploy_coordinator.register_connection(
            node_id,
            activate=lambda lease, hello=hello: registry.register(
                AsyncMock(), hello, lease.generation, lease.connection_token
            ),
        )
    return TestClient(Starlette(routes=create_api_routes(hub, store)))


async def _pending(store: EventStore, deploy_id: str, node: str = "n1", branch: str = "main", detected="2026-08-01T00:00:00Z"):
    await store.create_deploy_event(
        deploy_id=deploy_id,
        node_id=node,
        repo="r",
        branch=branch,
        commits=["c"],
        affected_services=[],
        diff_stat=None,
        detected_at=detected,
        target_head=deploy_id.rsplit(":", 1)[-1],
    )


async def test_repro_approve_all_skips_locked_requests_and_names_them(store: EventStore):
    await _pending(store, "n1:r:main:aaa")
    await _pending(store, "n1:r:dev:bbb", branch="dev")
    # main 쪽은 노드가 스스로 한 배포가 실패해 점검 필요가 켜졌다
    await store.record_node_deploy_report(node_report("failed", deploy_id="n1:r:main:aaa", node_id="n1"))
    client = await _bulk_client(store)

    response = client.post("/api/orch/approve-all")

    assert response.status_code == 200
    body = response.json()
    assert body["approved"] == ["n1:r:dev:bbb"]
    assert body["failed"] == []
    assert body["needs_inspection"] == [{"deploy_id": "n1:r:main:aaa", "reason": "점검한 뒤 하나씩 승인할 것"}]
    assert (await store.get_deploy_event("n1:r:main:aaa"))["status"] == "pending"
    assert await store.attempts.get_active_attempts() and len(await store.attempts.get_active_attempts()) == 1


async def test_approve_all_without_locks_keeps_the_old_response_keys(store: EventStore):
    await _pending(store, "n1:r:main:aaa")
    client = await _bulk_client(store)

    body = client.post("/api/orch/approve-all").json()

    assert set(body) == {"approved", "failed"} and body["approved"] == ["n1:r:main:aaa"]


async def test_approve_all_still_supersedes_older_requests_of_a_locked_branch(store: EventStore):
    await _pending(store, "n1:r:main:old", detected="2026-08-01T00:00:00Z")
    await _pending(store, "n1:r:main:new", detected="2026-08-02T00:00:00Z")
    await store.record_node_deploy_report(node_report("failed", deploy_id="n1:r:main:new", node_id="n1"))
    client = await _bulk_client(store)

    body = client.post("/api/orch/approve-all").json()

    assert body["approved"] == []
    assert [item["deploy_id"] for item in body["needs_inspection"]] == ["n1:r:main:new"]
    assert (await store.get_deploy_event("n1:r:main:old"))["reject_reason"] == "superseded by n1:r:main:new"


# ---------------------------------------------------------------- 대기 목록


async def test_repro_pending_list_carries_the_lock_and_the_last_failure(store: EventStore):
    await seed(store)
    hub = WebSocketHub(NodeRegistry(store), store, token="t")
    client = TestClient(Starlette(routes=create_api_routes(hub, store)))

    clean = client.get("/api/orch/pending").json()["deploys"][0]
    assert (clean["inspection_required"], clean["last_failure"]) == (False, None)

    await begin_and_fail(store)
    failed = client.get("/api/orch/pending").json()["deploys"][0]
    assert failed["inspection_required"] is True
    assert failed["last_failure"]["kind"] == "orchestrator_attempt"
    assert failed["last_failure"]["attempt_id"] == "failed-source"
    assert failed["last_failure"]["reason"] == "boom" and failed["last_failure"]["at"]
    assert {"deploy_id", "status", "commits", "affected_services"} <= set(failed)  # 기존 칸은 그대로

    await store.record_node_deploy_report(node_report("failed"))
    latest = client.get("/api/orch/pending").json()["deploys"][0]["last_failure"]
    assert (latest["kind"], latest["attempt_id"], latest["error"]) == ("node_deploy", "na1", "boom")


# ---------------------------------------------------------------- 약속 다 시험


async def _fail_commit(store: EventStore, harness: Harness, kind: str) -> None:
    """실제로 실행하다 실패한 커밋을 만든다."""
    if kind == "node_self_deploy_failure":
        repo = (await store.get_deploy_event(DEPLOY_ID))["repo"]
        await store.record_node_deploy_report(node_report("failed", repo=repo))
        return
    await store.attempts.begin_normal_attempt(
        orchestrator_attempt_id="failing",
        deploy_id=DEPLOY_ID,
        connection_generation=harness.generation,
        current_generation=harness.generation,
        source="manual_single",
        approved_by="director",
        deadline_at=future_deadline(),
    )
    if kind == "attempt_failure":
        await store.attempts.record_node_terminal(
            DeployAttemptTerminal(
                deploy_id=DEPLOY_ID,
                orchestrator_attempt_id="failing",
                connection_generation=harness.generation,
                kind="approval_revalidation_failed",
                reason="approval_revalidation_failed",
                error="boom",
            )
        )
    else:  # 실행 임대 마감
        attempt = await store.attempts.get_attempt("failing")
        await raw(store, "UPDATE deploy_attempts SET deadline_at = '2000-01-01T00:00:00+00:00'")
        result = await store.attempts.fail_attempt_if_deadline_due(
            "failing",
            expected_deadline_at="2000-01-01T00:00:00+00:00",
            kind="attempt_timeout",
            stage="deadline",
            reason="attempt_evidence_timeout",
            error="lease",
        )
        assert result["status"] == "failed" and attempt is not None


FAILURES = ["attempt_failure", "lease_deadline", "node_self_deploy_failure"]


async def _try_batch_probe(harness, event) -> None:
    """source가 있는 표시 줄: 일괄 승인은 탐침을 열 수는 있어도 답이 와도 시도를 열지 못한다."""
    task, probe = await _open_probe(harness, event, "manual_batch", "dashboard")
    await harness.handle_proposal(proposal_for(probe, harness, "fp-batch"))
    with pytest.raises(PlanRejected):
        await task


async def _try_batch_direct(harness, event) -> None:
    """source가 빈 표시 줄: 첫 승인 길이라 일괄 승인이 바로 거절된다."""
    with pytest.raises(PlanRejected) as exc:
        await harness.coordinator.approve_manual(event, approved_by="dashboard", source="manual_batch")
    assert (exc.value.status_code, str(exc.value)) == (409, "inspection_required")


@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize("path", ["auto_request", "batch_approval", "reopen_then_auto", "reopen_then_batch"])
async def test_promise_a_failed_commit_is_not_opened_until_a_single_approval_does(store: EventStore, failure, path):
    """약속 다: 실제로 실패한 커밋은 단건 승인이 새 시도를 열기 전에는 어느 길로도 시도가 열리지 않는다.

    범위 밖(시험하지 않는다): 요청 줄이 생기기 전에 온 스스로 한 배포의 실패(지금처럼 ignored, 2b와 PR 3 몫),
    옛 노드의 기동 받기가 auto_apply를 보지 않는 것(src/haniel/core/runner.py 2804-2811, 노드 몫).
    """
    reopen = path.startswith("reopen")
    await store.create_deploy_event(
        deploy_id=DEPLOY_ID,
        node_id="n",
        repo="haniel" if reopen else "r",
        branch="main",
        commits=["c"],
        affected_services=[],
        diff_stat=None,
        detected_at="2026-08-01T00:00:00Z",
        target_head="target",
        is_self_update=reopen,
    )
    event = await store.get_deploy_event(DEPLOY_ID)
    harness = Harness(store)
    try:
        await _fail_commit(store, harness, failure)
        assert await lock_of(store) == "l_on"
        if reopen:
            # 잠긴 채 거절된 자기 갱신이 되살아난다. 표시 줄은 두 시도 번호 칸이 빈 것으로 새로 선다
            await store.reject_deploy(DEPLOY_ID, "postpone")
            await store.set_deploy_postponed_until(DEPLOY_ID, "2000-01-01T00:00:00+00:00")
            assert await store.reopen_postponed_self_update(DEPLOY_ID) is True
            event = await store.get_deploy_event(DEPLOY_ID)
            assert await lock_of(store) == "l_on"
        blank = reopen or failure == "node_self_deploy_failure"
        sent_before = len(harness.sent)

        if "auto" in path:
            await harness.coordinator.handle_auto_request(auto_request("auto-try"))
            assert len(harness.sent) == sent_before + 1
            assert isinstance(harness.sent[-1][1], RejectedDeployAttemptAck)
        elif blank:
            await _try_batch_direct(harness, event)
        else:
            await _try_batch_probe(harness, event)

        assert await store.attempts.get_active_attempts() == []
        assert (await store.get_deploy_event(DEPLOY_ID))["status"] == "pending"
        assert await lock_of(store) == "l_on"
        # 대조: 단건 승인은 시도를 열고 점검 필요를 내린다
        if blank:
            await harness.coordinator.approve_manual(event, approved_by="director", source="manual_single")
        else:
            task, probe = await _open_probe(harness, event, "manual_single", "director")
            await harness.handle_proposal(proposal_for(probe, harness, "fp-single"))
            await task
        assert len(await store.attempts.get_active_attempts()) == 1
        assert await lock_of(store) == "l_off"
    finally:
        await harness.close()
