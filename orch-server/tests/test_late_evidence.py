"""닫힌 시도의 늦은 글, 시도 순서, 실행 시작 시각, 닫힌 뒤 적용 꼬리표(카드 #1259 PR 2b-1).

시도의 닫힘(outcome)과 증거 종결(sm_evidence)은 따로 움직인다. 오케스트레이터가 스스로 닫은 시도(실행 임대 마감,
보내기 실패)에 뒤늦게 닿은 결과와 정착은 그 시도의 증거로 적힌다. 증거가 성공으로 종결되면 「뒤 시도를 연 적이 없을 때만」
요청을 성공으로 닫고(완화 A), 실패로 종결되면 점검 필요를 켠다. 거절이나 밀림으로 닫힌 요청에 성공 증거가 닿으면
「닫힌 뒤 적용」 꼬리표를 남긴다.

`test_repro_`로 시작하는 것은 2a 판(바꾸기 전 커밋 9b95a18)에서 실패해야 하는 재현 시험이다.
"""

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from test_deploy_attempt_coordinator import Harness, auto_request, seed
from test_inspection_lock import (
    future_deadline,
    leaf_of,
    lineage_of,
    lock_of,
    marker_of,
    node_report,
    raw,
)
from haniel_orch.api import create_api_routes
from haniel_orch.event_store import EventStore
from haniel_orch.hub import WebSocketHub
from haniel_orch.node_registry import NodeRegistry
from haniel_orch.protocol import (
    DeployAttemptTerminal,
    DeployPlanProposal,
    DeployProgress,
    DeployResult,
    ManifestRecoveryEvidence,
    NodeHello,
    RepoReconciliation,
)
from haniel_orch.statechart.definitions import OrchRequest
from haniel_orch.statechart.restore import NoTransition, advance

DEPLOY_ID = "n:r:main:target"
PAST = "2000-01-01T00:00:00+00:00"


# ---------------------------------------------------------------- 도우미


async def begin(store: EventStore, attempt_id: str = "a1", generation: str = "g1") -> None:
    await store.attempts.begin_normal_attempt(
        orchestrator_attempt_id=attempt_id,
        deploy_id=DEPLOY_ID,
        connection_generation=generation,
        current_generation=generation,
        source="manual_single",
        approved_by="director",
        deadline_at=future_deadline(),
    )


async def lease_close(store: EventStore, attempt_id: str = "a1") -> None:
    """실행 임대 마감으로 시도를 닫는다(오케스트레이터가 스스로 내는 닫힘)."""
    await raw(store, "UPDATE deploy_attempts SET deadline_at = ? WHERE orchestrator_attempt_id = ?", PAST, attempt_id)
    result = await store.attempts.fail_attempt_if_deadline_due(
        attempt_id,
        expected_deadline_at=PAST,
        kind="attempt_timeout",
        stage="deadline",
        reason="attempt_evidence_timeout",
        error="lease",
    )
    assert result["status"] == "failed", result


async def begin_after_retry(store: EventStore, attempt_id: str = "a2", generation: str = "g1") -> None:
    """source가 있는 표시 줄의 점검 뒤 승인: 계획을 묻고 답을 받아 시도를 연다."""
    probe = await store.attempts.create_probe(
        probe_id=f"p-{attempt_id}",
        deploy_id=DEPLOY_ID,
        connection_generation=generation,
        deadline_at=future_deadline(),
        manual_retry=True,
    )
    proposal = DeployPlanProposal(
        mode="execute",
        probe_id=probe["probe_id"],
        connection_generation=generation,
        deploy_id=DEPLOY_ID,
        node_id="n",
        repo="r",
        branch="main",
        target_head="target",
        current_head="old",
        reason="legacy_retry",
        fingerprint=f"fp-{attempt_id}",
    )
    begun = await store.attempts.record_proposal_and_begin(
        proposal,
        orchestrator_attempt_id=attempt_id,
        source="manual_single",
        approved_by="director",
        deadline_at=future_deadline(),
        current_generation=generation,
    )
    assert begun["status"] == "begun", begun


async def late_result(
    store: EventStore, attempt_id: str = "a1", status: str = "success", error: str | None = None, generation: str = "g1"
):
    return await store.attempts.record_result(
        deploy_id=DEPLOY_ID,
        orchestrator_attempt_id=attempt_id,
        connection_generation=generation,
        status=status,
        error=error,
        duration_ms=7,
    )


async def late_settled(store: EventStore, attempt_id: str = "a1", local_head: str = "target", generation: str = "g1"):
    return await store.attempts.record_settled(
        deploy_id=DEPLOY_ID,
        orchestrator_attempt_id=attempt_id,
        connection_generation=generation,
        local_head=local_head,
        remote_head="target",
    )


# 2a 판에는 새 칸이 없다. 칸이 없으면 None으로 읽어, 재현 시험이 「칸이 없다」는 오류가 아니라 기대한 값과 다르다는
# 단언으로 실패하게 한다
async def attempt_row(store: EventStore, attempt_id: str) -> tuple:
    try:
        rows = await raw(
            store,
            "SELECT outcome, sm_evidence, late_evidence, execution_started_at, attempt_seq, terminal_kind "
            "FROM deploy_attempts WHERE orchestrator_attempt_id = ?",
            attempt_id,
        )
    except sqlite3.OperationalError:
        rows = await raw(
            store,
            "SELECT outcome, sm_evidence, NULL, NULL, NULL, terminal_kind FROM deploy_attempts "
            "WHERE orchestrator_attempt_id = ?",
            attempt_id,
        )
    return rows[0]


async def tag_of(store: EventStore, deploy_id: str = DEPLOY_ID) -> tuple:
    try:
        rows = await raw(
            store,
            "SELECT sm_tag, applied_after_close_at, applied_after_close_attempt_id FROM deploy_events WHERE deploy_id = ?",
            deploy_id,
        )
    except sqlite3.OperationalError:
        return (None, None, None)
    return rows[0]


def progress(attempt_id: str = "a1", node_id: str = "n", generation: str = "g1", budget: int | None = None) -> DeployProgress:
    return DeployProgress(
        deploy_id=DEPLOY_ID,
        node_id=node_id,
        orchestrator_attempt_id=attempt_id,
        connection_generation=generation,
        stage="build",
        expected_budget_sec=budget,
    )


def iso(moment: datetime) -> str:
    return moment.isoformat()


# ---------------------------------------------------------------- 정의의 한 걸음


@pytest.mark.parametrize(
    ("row", "facts", "expected"),
    [
        # 열린 대기: 뒤 시도를 연 적이 없으면 성공으로 닫고, 있으면 그대로(증거만)
        (("pending", "l_off", "t_off"), {"no_later": True}, ("success", "l_off", "t_off")),
        (("retry", "l_on", "t_off"), {"no_later": True}, ("success", "l_on", "t_off")),
        (("pending", "l_off", "t_off"), {"no_later": False}, None),
        (("retry", "l_on", "t_off"), {"no_later": False}, None),
        # 배포 중: 그대로(도는 시도가 있으면 사실은 거짓이다)
        (("deploying", "l_off", "t_off"), {"no_later": False}, None),
        (("deploying", "l_off", "t_off"), {"no_later": True}, None),
        # 거절과 밀림: 상태는 그대로 두고 꼬리표를 남긴다. 한 번 서면 내리지 않는다
        (("rejected", "l_on", "t_off"), {"no_later": False}, ("rejected", "l_on", "t_on")),
        (("superseded", "l_off", "t_off"), {"no_later": True}, ("superseded", "l_off", "t_on")),
        (("rejected", "l_on", "t_on"), {"no_later": False}, None),
        # 성공: 그대로
        (("success", "l_off", "t_off"), {"no_later": True}, None),
    ],
)
def test_success_evidence_definition_matrix(row, facts, expected):
    before = {"status": row[0], "lock": row[1], "tag": row[2]}
    if expected is None:
        with pytest.raises(NoTransition):
            advance(OrchRequest, before, "success_evidence", facts)
    else:
        assert advance(OrchRequest, before, "success_evidence", facts) == {
            "status": expected[0],
            "lock": expected[1],
            "tag": expected[2],
        }


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (("pending", "l_off"), ("retry", "l_on")),  # 처음 대기: retry로 가고 lock을 켠다
        (("retry", "l_off"), ("retry", "l_on")),  # retry: lock을 켠다
        (("retry", "l_on"), None),
        (("deploying", "l_off"), ("deploying", "l_on")),  # 배포 중: lock만 켜고 상태는 그대로
        (("rejected", "l_off"), ("rejected", "l_on")),  # 닫힘: lock만 켠다
        (("success", "l_off"), ("success", "l_on")),
        (("success", "l_on"), None),
    ],
)
def test_late_failed_definition_matrix(row, expected):
    before = {"status": row[0], "lock": row[1], "tag": "t_off"}
    if expected is None:
        with pytest.raises(NoTransition):
            advance(OrchRequest, before, "late_failed")
    else:
        assert advance(OrchRequest, before, "late_failed") == {
            "status": expected[0],
            "lock": expected[1],
            "tag": "t_off",
        }


def test_observed_needs_the_lock_off():
    facts = {"heads_equal": True, "no_active_attempt": True, "no_failed_node_attempt": True}
    assert advance(OrchRequest, {"status": "pending", "lock": "l_off", "tag": "t_off"}, "observed", facts)["status"] == "success"
    with pytest.raises(NoTransition):
        advance(OrchRequest, {"status": "pending", "lock": "l_on", "tag": "t_off"}, "observed", facts)


# ---------------------------------------------------------------- 어긋남 4: 닫힌 시도의 늦은 성공


async def test_repro_late_success_of_a_lease_closed_attempt_closes_the_request_when_no_later_attempt_was_opened(
    store: EventStore,
):
    await seed(store)
    await begin(store)
    await lease_close(store)
    assert await leaf_of(store) == ("pending", "retry", "l_on")

    assert await late_result(store) == {"status": "recorded"}
    assert await late_settled(store) == {"status": "success", "deploy_id": DEPLOY_ID}

    assert (await leaf_of(store))[:2] == ("success", "success")
    outcome, leaf, late, _started, _seq, kind = await attempt_row(store, "a1")
    assert (outcome, kind, leaf, late) == ("failed", "attempt_timeout", "ev_both", "success")  # 닫힘은 그대로, 증거만 종결
    assert await marker_of(store) is None and await lineage_of(store) == []
    assert await store.attempts.get_active_attempts() == []
    row = next(r for r in await store.get_deploy_history() if r["deploy_id"] == "attempt:a1")
    assert (row["status"], row["attempt_outcome"], row["late_evidence"]) == ("failed", "failed", "success")


async def test_repro_late_success_after_a_later_attempt_was_opened_is_evidence_only(store: EventStore):
    await seed(store)
    await begin(store)
    await lease_close(store)
    await begin_after_retry(store, "a2")
    assert await leaf_of(store) == ("deploying", "deploying", "l_off")

    assert await late_result(store) == {"status": "recorded"}
    result = await late_settled(store)

    assert result["status"] == "late_evidence" and result["evidence"] == "success"
    assert result["request_changed"] is False
    assert await leaf_of(store) == ("deploying", "deploying", "l_off")
    assert (await attempt_row(store, "a1"))[:3] == ("failed", "ev_both", "success")
    assert [a["orchestrator_attempt_id"] for a in await store.attempts.get_active_attempts()] == ["a2"]


async def test_repro_late_success_after_a_later_attempt_was_opened_and_closed_is_evidence_only(store: EventStore):
    """완화 A의 핵심: 뒤 시도(a2)를 연 적이 있으면, a2가 이미 닫혀 요청이 대기로 돌아왔어도 a1의 늦은 성공은 요청을 닫지 않는다.

    음성 대조: 「뒤 시도를 연 적이 없음」을 늘 참으로 바꾸면 이 시험이 실패한다(요청이 성공으로 닫힌다).
    """
    await seed(store)
    await begin(store)
    await lease_close(store)
    await begin_after_retry(store, "a2")
    assert (await late_result(store, attempt_id="a2", status="failed", error="a2 boom"))["status"] == "failed"
    assert await leaf_of(store) == ("pending", "retry", "l_on")

    assert await late_result(store) == {"status": "recorded"}
    result = await late_settled(store)

    assert result["status"] == "late_evidence" and result["evidence"] == "success"
    assert result["request_changed"] is False
    assert await leaf_of(store) == ("pending", "retry", "l_on")  # 요청은 그대로, 증거만
    assert (await attempt_row(store, "a1"))[:3] == ("failed", "ev_both", "success")
    assert (await attempt_row(store, "a2"))[:3] == ("failed", "ev_failed", None)
    assert (await marker_of(store))[:2] == ("a2", "a2")  # 표시 줄도 그대로


async def test_late_success_where_no_later_is_true_on_a_deploying_row_only_warns(store: EventStore, caplog):
    """배포 중인데 도는 시도가 없는 줄(옛 모양)에 「뒤 시도 없음」이 참으로 들어오면 경고만 남기고 요청은 그대로다."""
    await seed(store)
    await begin(store)
    await lease_close(store)
    await raw(store, "UPDATE deploy_events SET status = 'deploying', sm_status = 'deploying'")
    caplog.set_level(logging.WARNING)

    await late_result(store)
    result = await late_settled(store)

    assert result["status"] == "late_evidence" and result["request_changed"] is False
    assert (await leaf_of(store))[:2] == ("deploying", "deploying")
    assert any("no_later" in r.getMessage() and DEPLOY_ID in r.getMessage() for r in caplog.records)


async def test_late_success_settled_first_then_result_closes_the_same_way(store: EventStore):
    await seed(store)
    await begin(store)
    await lease_close(store)

    assert await late_settled(store) == {"status": "recorded"}
    assert await late_result(store) == {"status": "success", "deploy_id": DEPLOY_ID}
    assert (await leaf_of(store))[:2] == ("success", "success")


async def test_a_second_late_report_after_the_evidence_closed_is_ignored(store: EventStore):
    await seed(store)
    await begin(store)
    await lease_close(store)
    await late_result(store)
    await late_settled(store)

    assert await late_result(store) == {"status": "ignored"}
    assert await late_settled(store) == {"status": "ignored"}
    assert (await attempt_row(store, "a1"))[:3] == ("failed", "ev_both", "success")


async def test_late_reports_to_a_node_closed_attempt_stay_ignored(store: EventStore):
    """노드 글(실패 결과)로 닫힌 시도는 증거도 함께 종결됐으므로 늦은 글은 지금처럼 ignored다."""
    await seed(store)
    await begin(store)
    assert (await late_result(store, status="failed", error="boom"))["status"] == "failed"
    assert (await attempt_row(store, "a1"))[:3] == ("failed", "ev_failed", None)

    assert await late_result(store) == {"status": "ignored"}
    assert await late_settled(store) == {"status": "ignored"}
    assert await leaf_of(store) == ("pending", "retry", "l_on")


async def test_late_recovery_evidence_and_late_terminal_stay_ignored(store: EventStore):
    await seed(store)
    await begin(store)
    await lease_close(store)

    terminal = await store.attempts.record_node_terminal(
        DeployAttemptTerminal(
            deploy_id=DEPLOY_ID,
            orchestrator_attempt_id="a1",
            connection_generation="g1",
            kind="approval_revalidation_failed",
            reason="approval_revalidation_failed",
            error="late",
        )
    )
    evidence = await store.attempts.record_recovery_evidence(
        ManifestRecoveryEvidence(
            deploy_id=DEPLOY_ID,
            orchestrator_attempt_id="a1",
            source_orchestrator_attempt_id="a0",
            journal_attempt_id="j",
            connection_generation="g1",
            node_id="n",
            repo="r",
            branch="main",
            target_head="target",
            current_head="target",
            manifest_identity="m",
            manifest_digest="d",
            journal_status="success",
            journal_completed_at="2026-08-01T00:00:00Z",
        )
    )
    assert terminal == {"status": "ignored"} and evidence == {"status": "ignored"}
    assert (await attempt_row(store, "a1"))[:3] == ("failed", "ev_none", None)


async def test_a_contradicting_late_report_is_ignored(store: EventStore):
    """다른 요청 번호나 다른 세대를 단 글은 모순되는 글이라 적지 않는다."""
    await seed(store)
    await begin(store)
    await lease_close(store)

    other_generation = await late_result(store, generation="g9")
    other_request = await store.attempts.record_result(
        deploy_id="n:r:main:other",
        orchestrator_attempt_id="a1",
        connection_generation="g1",
        status="success",
        error=None,
        duration_ms=1,
    )
    assert other_generation == other_request == {"status": "ignored"}
    assert (await attempt_row(store, "a1"))[:3] == ("failed", "ev_none", None)


# ---------------------------------------------------------------- 늦은 실패


async def test_repro_late_failure_on_a_retry_turns_the_lock_on_and_records_failed_evidence(store: EventStore):
    await seed(store)
    await begin(store)
    await lease_close(store)
    await raw(store, "UPDATE deploy_events SET sm_lock = 'l_off'")  # 점검 필요만 내려간 줄(되돌렸다 올린 DB를 흉내 낸다)
    marker_before = await marker_of(store)

    result = await late_result(store, status="failed", error="late boom")

    assert result["status"] == "late_evidence" and result["evidence"] == "failed"
    assert result["request_changed"] is True
    assert await leaf_of(store) == ("pending", "retry", "l_on")
    assert (await attempt_row(store, "a1"))[:3] == ("failed", "ev_failed", "failed")
    assert await marker_of(store) == (marker_before[0], marker_before[1], 1)  # source와 마지막 실패는 그대로


async def test_repro_late_failure_while_a_later_attempt_runs_only_turns_the_lock_on(store: EventStore):
    await seed(store)
    await begin(store)
    await lease_close(store)
    await begin_after_retry(store, "a2")
    assert await leaf_of(store) == ("deploying", "deploying", "l_off")
    updated_before = (await store.get_deploy_event(DEPLOY_ID))["updated_at"]

    result = await late_result(store, status="failed", error="late boom")

    assert result["status"] == "late_evidence" and result["evidence"] == "failed"
    assert await leaf_of(store) == ("deploying", "deploying", "l_on")
    assert (await store.get_deploy_event(DEPLOY_ID))["updated_at"] == updated_before
    assert [a["orchestrator_attempt_id"] for a in await store.attempts.get_active_attempts()] == ["a2"]
    assert (await attempt_row(store, "a1"))[:3] == ("failed", "ev_failed", "failed")


async def test_late_failure_on_a_closed_request_only_turns_the_lock_on(store: EventStore):
    await seed(store)
    await begin(store)
    await lease_close(store)
    await store.reject_deploy(DEPLOY_ID, "no")
    await raw(store, "UPDATE deploy_events SET sm_lock = 'l_off'")
    before = (await raw(store, "SELECT status, reject_reason, updated_at FROM deploy_events"))[0]

    result = await late_result(store, status="failed", error="late boom")

    assert result["status"] == "late_evidence" and result["evidence"] == "failed"
    assert (await raw(store, "SELECT status, reject_reason, updated_at FROM deploy_events"))[0] == before
    assert await lock_of(store) == "l_on"
    assert await marker_of(store) is None


async def test_late_failure_on_a_first_pending_request_makes_a_sourced_marker(store: EventStore):
    """처음 대기 줄에 닿은 늦은 실패: retry로 가고, 실행한 시도를 source로 둔 표시 줄과 계보를 만든다."""
    await seed(store)
    await begin(store)
    await lease_close(store)
    # 닫힌 시도가 있는 처음 대기 줄(표시 줄 없음)을 만든다. 실제로는 잠금 없이 되살아난 자기 갱신 줄이 이 모양이다
    await raw(store, "DELETE FROM deploy_retry_requirements")
    await raw(store, "DELETE FROM deploy_retry_source_attempts")
    await raw(store, "UPDATE deploy_events SET sm_status = 'pending', sm_lock = 'l_off'")

    result = await late_result(store, status="failed", error="late boom")

    assert result["status"] == "late_evidence" and result["request_changed"] is True
    assert await leaf_of(store) == ("pending", "retry", "l_on")
    assert await marker_of(store) == ("a1", "a1", 1)
    assert await lineage_of(store) == ["a1"]


async def test_late_settled_with_a_wrong_head_is_a_late_failure(store: EventStore):
    await seed(store)
    await begin(store)
    await lease_close(store)
    await raw(store, "UPDATE deploy_events SET sm_lock = 'l_off'")

    result = await late_settled(store, local_head="wrong")

    assert result["status"] == "late_evidence" and result["evidence"] == "failed"
    assert (await attempt_row(store, "a1"))[:3] == ("failed", "ev_failed", "failed")
    assert await lock_of(store) == "l_on"


# ---------------------------------------------------------------- 어긋남 5: 진행 알림의 시도 번호 비교와 소유권


async def test_repro_progress_after_reconnect_renews_the_lease_of_the_same_attempt(store: EventStore):
    await seed(store)
    harness = Harness(store)
    try:
        await begin(store, generation=harness.generation)
        before = (await store.attempts.get_attempt("a1"))["deadline_at"]
        await harness.disconnect_current()
        await harness.reconnect()
        assert harness.generation != "g1"

        await harness.coordinator.handle_progress(progress(generation="g1"))  # 노드는 허가의 세대를 되돌려 보낸다

        after = (await store.attempts.get_attempt("a1"))["deadline_at"]
        assert after != before
        assert datetime.fromisoformat(after) > datetime.now(timezone.utc)
    finally:
        await harness.close()


async def test_progress_of_another_node_or_another_attempt_does_not_renew(store: EventStore):
    await seed(store)
    harness = Harness(store)
    try:
        await begin(store, generation=harness.generation)
        before = (await store.attempts.get_attempt("a1"))["deadline_at"]

        await harness.coordinator.handle_progress(progress(node_id="n2"))  # 시도 줄의 노드와 다르다
        await harness.coordinator.handle_progress(progress(attempt_id="a9"))  # 없는 시도

        assert (await store.attempts.get_attempt("a1"))["deadline_at"] == before
    finally:
        await harness.close()


async def _connected_hub(store: EventStore, *node_ids: str):
    registry = NodeRegistry(store)
    hub = WebSocketHub(registry, store, token="t", deploy_timeout_sec=60)
    generations: dict[str, str] = {}
    for node_id in node_ids:
        hello = NodeHello(node_id=node_id, token="t", hostname=f"h-{node_id}", os="Linux", arch="x86_64", haniel_version="0.1.0")
        lease = await hub.deploy_coordinator.register_connection(
            node_id,
            activate=lambda lease, hello=hello: registry.register(AsyncMock(), hello, lease.generation, lease.connection_token),
        )
        generations[node_id] = lease.generation
    return hub, generations


async def _run_node_socket(hub: WebSocketHub, node_id: str, *messages) -> None:
    hello = NodeHello(node_id=node_id, token="t", hostname=f"h-{node_id}", os="Linux", arch="x86_64", haniel_version="0.1.0")
    websocket = AsyncMock()
    websocket.receive_text.side_effect = [
        hello.model_dump_json(),
        *(message.model_dump_json() for message in messages),
        WebSocketDisconnect(code=1000),
    ]
    await hub.handle_node_ws(websocket)


async def test_repro_progress_from_another_node_connection_is_dropped(store: EventStore, caplog):
    """n2 연결이 n의 시도에 n의 이름으로 진행 알림을 보낸다. 본문 node_id가 연결과 다르면 경고하고 버린다."""
    await seed(store)
    hub, generations = await _connected_hub(store, "n")
    await begin(store, generation=generations["n"])
    before = (await store.attempts.get_attempt("a1"))["deadline_at"]
    caplog.set_level(logging.WARNING)

    await _run_node_socket(hub, "n2", progress(node_id="n", generation=generations["n"]))

    assert (await store.attempts.get_attempt("a1"))["deadline_at"] == before
    assert any("n2" in r.getMessage() and "deploy_progress" in r.getMessage() for r in caplog.records)
    await hub.shutdown()


async def test_repro_result_from_another_node_connection_is_dropped(store: EventStore):
    await seed(store)
    hub, generations = await _connected_hub(store, "n")
    await begin(store, generation=generations["n"])

    await _run_node_socket(
        hub,
        "n2",
        DeployResult(
            deploy_id=DEPLOY_ID,
            node_id="n",
            status="success",
            duration_ms=1,
            orchestrator_attempt_id="a1",
            connection_generation=generations["n"],
        ),
        RepoReconciliation(
            phase="settled",
            deploy_id=DEPLOY_ID,
            node_id="n",
            repo="r",
            branch="main",
            local_head="target",
            remote_head="target",
            orchestrator_attempt_id="a1",
            connection_generation=generations["n"],
        ),
    )

    assert (await attempt_row(store, "a1"))[:2] == ("active", "ev_none")
    assert (await leaf_of(store))[:2] == ("deploying", "deploying")
    await hub.shutdown()


async def test_progress_for_a_closed_attempt_only_notes_the_execution_start(store: EventStore):
    await seed(store)
    await begin(store)
    await lease_close(store)
    before = await store.attempts.get_attempt("a1")

    result = await store.attempts.record_progress(
        deploy_id=DEPLOY_ID,
        orchestrator_attempt_id="a1",
        node_id="n",
        expected_budget_sec=None,
        deadline_at=future_deadline(),
    )

    after = await store.attempts.get_attempt("a1")
    assert result == {"status": "closed"}
    assert after["outcome"] == "failed" and after["deadline_at"] == before["deadline_at"]
    assert after["execution_started_at"] is not None


# ---------------------------------------------------------------- 어긋남 6: 실행 상한은 실행 시작 시각에서 잰다


async def test_repro_hard_deadline_is_measured_from_execution_started_at(store: EventStore):
    """승인 뒤 200초가 지나 첫 알림이 왔다(옛 노드는 앞 배포 동안 승인을 읽지 못한다). 상한은 4분이다(시험 하니스 60초 × 4).

    첫 알림은 실행 시작 시각이 없어 지금처럼 started_at에서 재고(상한 = 승인 + 240초), 둘째 알림부터는
    execution_started_at에서 재므로 상한이 멀어져 늘 60초 임대가 그대로 들어간다.
    """
    await seed(store)
    harness = Harness(store)
    try:
        await begin(store, generation=harness.generation)
        now = datetime.now(timezone.utc)
        await raw(store, "UPDATE deploy_attempts SET started_at = ? WHERE orchestrator_attempt_id = 'a1'", iso(now - timedelta(seconds=200)))

        await harness.coordinator.handle_progress(progress())
        first = datetime.fromisoformat((await store.attempts.get_attempt("a1"))["deadline_at"])
        await harness.coordinator.handle_progress(progress())
        second = datetime.fromisoformat((await store.attempts.get_attempt("a1"))["deadline_at"])

        assert first <= now + timedelta(seconds=41)  # 승인 + 240초 = 지금 + 40초
        assert second >= now + timedelta(seconds=59)  # 실행 시작 + 240초가 멀어 60초 임대가 그대로
        assert (await store.attempts.get_attempt("a1"))["execution_started_at"] is not None
    finally:
        await harness.close()


async def _fresh_attempt(store: EventStore, deploy_id: str, attempt_id: str) -> None:
    await store.create_deploy_event(
        deploy_id=deploy_id,
        node_id="n",
        repo=deploy_id.split(":")[1],
        branch="main",
        commits=["c"],
        affected_services=[],
        diff_stat=None,
        detected_at="2026-08-01T00:00:00Z",
        target_head="target",
    )
    await store.attempts.begin_normal_attempt(
        orchestrator_attempt_id=attempt_id,
        deploy_id=deploy_id,
        connection_generation="g1",
        current_generation="g1",
        source="manual_single",
        approved_by="director",
        deadline_at=future_deadline(),
    )


async def _send(store: EventStore, kind: str, deploy_id: str, attempt_id: str):
    attempts = store.attempts
    if kind == "progress":
        return await attempts.record_progress(
            deploy_id=deploy_id, orchestrator_attempt_id=attempt_id, node_id="n", expected_budget_sec=None, deadline_at=future_deadline()
        )
    if kind == "result":
        return await attempts.record_result(
            deploy_id=deploy_id, orchestrator_attempt_id=attempt_id, connection_generation="g1", status="success", error=None, duration_ms=1
        )
    if kind == "failed_result":
        return await attempts.record_result(
            deploy_id=deploy_id, orchestrator_attempt_id=attempt_id, connection_generation="g1", status="failed", error="e", duration_ms=1
        )
    if kind == "settled":
        return await attempts.record_settled(
            deploy_id=deploy_id, orchestrator_attempt_id=attempt_id, connection_generation="g1", local_head="target", remote_head="target"
        )
    if kind == "terminal":
        return await attempts.record_node_terminal(
            DeployAttemptTerminal(
                deploy_id=deploy_id,
                orchestrator_attempt_id=attempt_id,
                connection_generation="g1",
                kind="approval_revalidation_failed",
                reason="approval_revalidation_failed",
                error="x",
            )
        )
    if kind == "recovery_evidence":
        return await attempts.record_recovery_evidence(
            ManifestRecoveryEvidence(
                deploy_id=deploy_id,
                orchestrator_attempt_id=attempt_id,
                source_orchestrator_attempt_id="a0",
                journal_attempt_id="j",
                connection_generation="g1",
                node_id="n",
                repo=deploy_id.split(":")[1],
                branch="main",
                target_head="target",
                current_head="target",
                manifest_identity="m",
                manifest_digest="d",
                journal_status="success",
                journal_completed_at="2026-08-01T00:00:00Z",
            )
        )
    if kind == "deadline":
        await raw(store, "UPDATE deploy_attempts SET deadline_at = ? WHERE orchestrator_attempt_id = ?", PAST, attempt_id)
        return await attempts.fail_attempt_if_deadline_due(
            attempt_id, expected_deadline_at=PAST, kind="attempt_timeout", stage="deadline", reason="attempt_evidence_timeout", error="l"
        )
    if kind == "not_sent":
        return await attempts.fail_attempt_not_sent(attempt_id)
    raise AssertionError(kind)


@pytest.mark.parametrize(
    ("kind", "written"),
    [
        ("progress", True),
        ("result", True),
        ("failed_result", True),
        ("settled", True),
        ("terminal", False),
        ("recovery_evidence", False),
        ("deadline", False),
        ("not_sent", False),
    ],
)
async def test_execution_started_at_is_written_only_by_execution_evidence(store: EventStore, kind, written):
    deploy_id = f"n:{kind}:main:target"
    await _fresh_attempt(store, deploy_id, kind)

    await _send(store, kind, deploy_id, kind)

    assert ((await attempt_row(store, kind))[3] is not None) is written, kind


async def test_execution_started_at_is_written_once(store: EventStore):
    deploy_id = "n:once:main:target"
    await _fresh_attempt(store, deploy_id, "once")
    await _send(store, "progress", deploy_id, "once")
    first = (await attempt_row(store, "once"))[3]
    await asyncio.sleep(0.002)

    await _send(store, "result", deploy_id, "once")
    await _send(store, "settled", deploy_id, "once")

    assert (await attempt_row(store, "once"))[3] == first


async def test_a_late_result_to_a_closed_attempt_still_writes_the_execution_start(store: EventStore):
    await seed(store)
    await begin(store)
    await lease_close(store)
    assert (await attempt_row(store, "a1"))[3] is None

    await late_result(store)

    assert (await attempt_row(store, "a1"))[3] is not None


# ---------------------------------------------------------------- 시도 순서 칸


async def test_repro_attempt_seq_counts_per_request_from_one(store: EventStore):
    await seed(store)
    await begin(store)
    await lease_close(store)
    await begin_after_retry(store, "a2")
    await _fresh_attempt(store, "n:other:main:target", "o1")

    assert (await attempt_row(store, "a1"))[4] == 1
    assert (await attempt_row(store, "a2"))[4] == 2
    assert (await attempt_row(store, "o1"))[4] == 1


async def test_an_auto_path_attempt_gets_its_seq_from_the_orchestrator(store: EventStore):
    await seed(store)
    harness = Harness(store)
    try:
        await harness.coordinator.handle_auto_request(auto_request("auto-1"))
        probe = harness.sent[-1][1]
        await harness.handle_proposal(
            DeployPlanProposal(
                mode="execute",
                probe_id=probe.probe_id,
                connection_generation=harness.generation,
                deploy_id=DEPLOY_ID,
                node_id="n",
                repo="r",
                branch="main",
                target_head="target",
                current_head="old",
                reason="normal_pull",
                fingerprint="fp-auto",
            )
        )
        assert (await attempt_row(store, "auto-1"))[4] == 1
    finally:
        await harness.close()


async def test_a_closed_attempt_without_a_seq_is_treated_as_having_a_later_attempt(store: EventStore):
    """순서 칸이 비었거나 견줄 수 없으면 「뒤 시도를 연 적이 있다」로 본다(요청을 닫지 않는 쪽)."""
    await seed(store)
    await begin(store)
    await lease_close(store)
    await raw(store, "UPDATE deploy_attempts SET attempt_seq = NULL")

    await late_result(store)
    result = await late_settled(store)

    assert result["status"] == "late_evidence" and result["request_changed"] is False
    assert await leaf_of(store) == ("pending", "retry", "l_on")


# ---------------------------------------------------------------- 어긋남 12: 닫힌 뒤 적용 꼬리표


def _client(store: EventStore) -> TestClient:
    hub = WebSocketHub(NodeRegistry(store), store, token="t")
    return TestClient(Starlette(routes=create_api_routes(hub, store)))


async def test_repro_late_success_on_a_rejected_request_leaves_the_applied_after_close_tag(store: EventStore):
    await seed(store)
    await begin(store)
    await lease_close(store)
    await store.reject_deploy(DEPLOY_ID, "no")
    assert await tag_of(store) == ("t_off", None, None)

    assert await late_result(store) == {"status": "recorded"}
    result = await late_settled(store)

    assert result["status"] == "late_evidence" and result["request_changed"] is True
    tag, at, attempt_id = await tag_of(store)
    assert (tag, attempt_id) == ("t_on", "a1") and at
    assert (await raw(store, "SELECT status, reject_reason FROM deploy_events"))[0] == ("rejected", "no")
    item = next(r for r in _client(store).get("/api/orch/history").json()["deploys"] if r["deploy_id"] == DEPLOY_ID)
    assert (item["applied_after_close"], item["applied_after_close_at"], item["applied_after_close_attempt_id"]) == (True, at, "a1")

    # 같은 글이 다시 오면 ignored이고 꼬리표는 그대로다
    assert await late_settled(store) == {"status": "ignored"}
    assert await tag_of(store) == ("t_on", at, "a1")


async def test_repro_node_self_deploy_success_on_a_rejected_request_leaves_the_tag(store: EventStore):
    await seed(store)
    await store.reject_deploy(DEPLOY_ID, "no")

    result = await store.record_node_deploy_report(node_report("succeeded", node_attempt_id="na7"))

    assert result["status"] == "ignored"  # 답은 지금 그대로
    tag, at, attempt_id = await tag_of(store)
    assert (tag, attempt_id) == ("t_on", "na7") and at
    assert (await raw(store, "SELECT status FROM deploy_events"))[0] == ("rejected",)
    assert await raw(store, "SELECT 1 FROM node_deploy_attempts") == []  # 노드 시도 줄은 지금처럼 만들지 않는다


async def test_late_success_on_a_superseded_request_leaves_the_tag(store: EventStore):
    await seed(store)
    await begin(store)
    await lease_close(store)
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
    assert (await leaf_of(store))[:2] == ("rejected", "superseded")

    await late_result(store)
    await late_settled(store)

    assert (await tag_of(store))[0] == "t_on"
    assert (await leaf_of(store))[:2] == ("rejected", "superseded")


async def test_a_node_failure_or_a_late_failure_on_a_rejected_request_leaves_no_tag(store: EventStore, caplog):
    """거절된 요청(점검 필요는 이미 켜짐)에 온 늦은 실패는 요청 정의에 전이가 없어 NoTransition으로 끝나지만,
    증거 걸음은 그 앞에서 커밋되므로 시도 줄의 증거(ev_failed, late_evidence=failed)는 남는다(goal 5d)."""
    await seed(store)
    await begin(store)
    await lease_close(store)
    await store.reject_deploy(DEPLOY_ID, "no")
    caplog.set_level(logging.WARNING)

    await store.record_node_deploy_report(node_report("failed"))
    result = await late_result(store, status="failed", error="late")

    assert result["status"] == "late_evidence" and result["request_changed"] is False
    assert (await attempt_row(store, "a1"))[:3] == ("failed", "ev_failed", "failed")
    assert (await leaf_of(store)) == ("rejected", "rejected", "l_on")
    assert (await tag_of(store))[0] == "t_off"
    assert any("late_failed" in r.getMessage() and "no_transition" in r.getMessage() for r in caplog.records)


async def test_pending_and_history_items_carry_applied_after_close_false_by_default(store: EventStore):
    await seed(store)
    client = _client(store)

    pending = client.get("/api/orch/pending").json()["deploys"][0]
    history = client.get("/api/orch/history").json()["deploys"][0]

    for item in (pending, history):
        assert (item["applied_after_close"], item["applied_after_close_at"], item["applied_after_close_attempt_id"]) == (
            False,
            None,
            None,
        )


# ---------------------------------------------------------------- 이력의 late_evidence


async def test_history_late_evidence_is_null_unless_evidence_arrived_after_the_closure(store: EventStore):
    await seed(store)
    await begin(store)
    await lease_close(store)  # 스스로 닫았고 증거는 아직 없다
    await _fresh_attempt(store, "n:node:main:target", "by-node")
    await _send(store, "failed_result", "n:node:main:target", "by-node")  # 노드 글로 닫혔다

    rows = {r["deploy_id"]: r for r in await store.get_deploy_history()}

    assert rows["attempt:a1"]["late_evidence"] is None
    assert rows["attempt:by-node"]["late_evidence"] is None
