"""점검 필요 알림 원장 시험(카드 #1260).

잠금이 꺼짐에서 켜짐으로 바뀌는 여덟 사건이 같은 트랜잭션에 원장 한 줄을 남기는지, 잠금이 유지되면
새 줄이 없는지, 풀렸다 다시 잠기면 새 줄이 있는지, 롤백이면 줄이 없는지, 기동 보충과 본문 가림을 본다.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from haniel_orch.api import create_api_routes
from haniel_orch.deploy_attempt_schema import initialize_attempt_schema
from haniel_orch.event_store import EventStore
from haniel_orch.hub import WebSocketHub
from haniel_orch.node_registry import NodeRegistry
from haniel_orch.statechart import inspection_notice, steps
from haniel_orch.statechart.inspection_notice_text import (
    INSTRUCTION_TEXT,
    redact_secrets,
    render_notice_text,
)

import state_support as support

DEPLOY_ID = "n:r:main:target"
CLOSING_DATA = {
    "failed_attempt_id": "a1",
    "stable_source": "a1",
    "marker_created_at": None,
}
LOCKING_EVENTS = [
    "plan_failed_journal",
    "attempt_failed",
    "send_failed",
    "lease_deadline",
    "grace_deadline",
    "hello_missing",
    "node_failed",
    "late_failed",
]


async def new_request(
    store: EventStore,
    deploy_id: str = DEPLOY_ID,
    *,
    node_id: str = "n",
    repo: str = "r",
    branch: str = "main",
    target_head: str = "target",
) -> None:
    await store.create_deploy_event(
        deploy_id=deploy_id,
        node_id=node_id,
        repo=repo,
        branch=branch,
        commits=["target change"],
        affected_services=[],
        diff_stat=None,
        detected_at="2026-08-01T00:00:00Z",
        target_head=target_head,
    )


async def begin_deploying(
    store: EventStore, deploy_id: str = DEPLOY_ID, belief: str = "b_unknown"
) -> None:
    await support.fire(
        store,
        deploy_id,
        "begin_attempt",
        facts={"manual_single": True, "via_probe": False},
        data={"approved_by": "director"},
    )
    if belief in ("b_queued", "b_running"):
        await support.fire(store, deploy_id, "acked")
    if belief == "b_running":
        await support.fire(store, deploy_id, "started")


async def fire_locking_event(
    store: EventStore, event: str, deploy_id: str = DEPLOY_ID
) -> None:
    """여덟 잠금 사건을 각각 잠금이 꺼진 요청에 넣는다."""
    if event == "plan_failed_journal":
        await support.fire(store, deploy_id, event, data=CLOSING_DATA)
    elif event == "late_failed":
        await support.fire(store, deploy_id, event, data=CLOSING_DATA)
    elif event == "node_failed":
        await begin_deploying(store, deploy_id)
        await support.fire(
            store, deploy_id, event, facts={"no_active_attempt": True}
        )
    elif event == "attempt_failed":
        await begin_deploying(store, deploy_id)
        await support.fire(store, deploy_id, event, facts={"newer": False}, data=CLOSING_DATA)
    elif event == "send_failed":
        await begin_deploying(store, deploy_id)
        await support.fire(
            store,
            deploy_id,
            event,
            facts={"newer": False, "legacy": True},
            data=CLOSING_DATA,
        )
    elif event == "lease_deadline":
        await begin_deploying(store, deploy_id, belief="b_running")
        await support.fire(store, deploy_id, event, facts={"newer": False}, data=CLOSING_DATA)
    elif event == "grace_deadline":
        await begin_deploying(store, deploy_id, belief="b_queued")
        await support.fire(store, deploy_id, event, facts={"newer": False}, data=CLOSING_DATA)
    elif event == "hello_missing":
        await begin_deploying(store, deploy_id, belief="b_running")
        await support.fire(store, deploy_id, event, facts={"newer": False}, data=CLOSING_DATA)
    else:  # pragma: no cover - 시험 목록이 틀렸을 때만
        raise AssertionError(f"알 수 없는 잠금 사건: {event}")


async def notice_rows(
    store: EventStore, deploy_id: str | None = None
) -> list[dict]:
    if deploy_id is None:
        cursor = await store._db.execute(
            "SELECT * FROM inspection_notices ORDER BY created_at, rowid"
        )
    else:
        cursor = await store._db.execute(
            "SELECT * FROM inspection_notices WHERE deploy_id = ? ORDER BY created_at, rowid",
            (deploy_id,),
        )
    names = [description[0] for description in cursor.description]
    return [dict(zip(names, row)) for row in await cursor.fetchall()]


async def leaf(store: EventStore, deploy_id: str = DEPLOY_ID) -> tuple:
    cursor = await store._db.execute(
        "SELECT status, sm_status, sm_lock FROM deploy_events WHERE deploy_id = ?",
        (deploy_id,),
    )
    return tuple(await cursor.fetchone())


# ---------------------------------------------------------------- 잠금 사건 여덟


@pytest.mark.parametrize("event", LOCKING_EVENTS)
async def test_each_locking_event_records_one_notice(store: EventStore, event: str):
    await new_request(store)

    await fire_locking_event(store, event)

    rows = await notice_rows(store)
    assert len(rows) == 1
    assert rows[0]["event"] == event
    assert rows[0]["state"] == "unsent"
    assert (rows[0]["node_id"], rows[0]["repo"], rows[0]["branch"]) == ("n", "r", "main")
    assert rows[0]["target_head"] == "target"
    assert rows[0]["sm_status"] == "retry"
    assert await leaf(store) == ("pending", "retry", "l_on")


async def test_lock_staying_on_does_not_add_another_notice(store: EventStore):
    await new_request(store)
    await fire_locking_event(store, "plan_failed_journal")

    # retry -> retry, l_on -> l_on: 잠금이 그대로면 새 줄이 없다
    await support.fire(store, DEPLOY_ID, "plan_failed_journal", data=CLOSING_DATA)

    assert len(await notice_rows(store)) == 1


async def test_unlock_then_relock_records_a_new_notice(store: EventStore):
    await new_request(store)
    await fire_locking_event(store, "plan_failed_journal")
    await begin_deploying(store)
    assert await leaf(store) == ("deploying", "deploying", "l_off")

    await support.fire(store, DEPLOY_ID, "attempt_failed", facts={"newer": False}, data=CLOSING_DATA)

    rows = await notice_rows(store)
    assert len(rows) == 2
    assert rows[1]["event"] == "attempt_failed"
    # 같은 요청이 전에 알림을 받은 횟수를 그때 세어 둔다
    assert rows[1]["prior_notice_count"] == 1


async def test_recording_wakes_the_sender(store: EventStore):
    calls: list[int] = []

    def listener() -> None:
        calls.append(1)

    inspection_notice.register_wake_listener(listener)
    try:
        await new_request(store)
        await fire_locking_event(store, "plan_failed_journal")
    finally:
        inspection_notice.unregister_wake_listener(listener)

    # 보내기 루프는 이 깨우기로 커밋 직후 바로 한 바퀴 돈다
    assert calls == [1]


async def test_rollback_leaves_no_notice(store: EventStore):
    await new_request(store)

    async with store._mutation_lock:
        await steps.step_request(store._db, DEPLOY_ID, "plan_failed_journal", data=CLOSING_DATA)
        await store._db.rollback()

    assert await notice_rows(store) == []
    assert await leaf(store) == ("pending", "pending", "l_off")


async def test_notice_is_recorded_when_the_lock_turns_on_as_the_request_closes(
    store: EventStore,
):
    await new_request(store)
    await begin_deploying(store)

    # 밀리며 닫히는 같은 걸음에 잠금이 켜진다. 원장 줄은 닫힘 정리보다 먼저 적혀야 남는다
    await support.fire(
        store, DEPLOY_ID, "attempt_failed", facts={"newer": True}, data=CLOSING_DATA
    )

    assert await leaf(store) == ("rejected", "superseded", "l_on")
    rows = await notice_rows(store)
    assert len(rows) == 1
    assert rows[0]["sm_status"] == "superseded"


# ---------------------------------------------------------------- 기동 보충


async def test_startup_backfill_fills_locked_open_requests_without_a_notice(
    store: EventStore,
):
    await new_request(store)
    await store._db.execute(
        "UPDATE deploy_events SET sm_lock = 'l_on', sm_status = 'retry' WHERE deploy_id = ?",
        (DEPLOY_ID,),
    )
    await store._db.commit()

    assert await store.record_startup_inspection_notices() == 1
    rows = await notice_rows(store)
    assert rows[0]["event"] == "startup_backfill"
    assert rows[0]["sm_status"] == "retry"

    # 두 번 돌아도 같은 요청에 또 적지 않는다
    assert await store.record_startup_inspection_notices() == 0
    assert len(await notice_rows(store)) == 1


async def test_startup_backfill_skips_unlocked_open_requests(store: EventStore):
    await new_request(store)

    assert await store.record_startup_inspection_notices() == 0
    assert await notice_rows(store) == []


async def test_startup_backfill_covers_locks_turned_on_by_leaf_backfill(
    store: EventStore,
):
    await new_request(store)
    # 옛 코드가 남긴 모양: 잎이 비었고 점검 필요 표시만 켜져 있다
    await store._db.execute(
        "UPDATE deploy_events SET sm_status = NULL, sm_lock = NULL WHERE deploy_id = ?",
        (DEPLOY_ID,),
    )
    await store._db.execute(
        "INSERT INTO deploy_retry_requirements "
        "(deploy_id, created_at, updated_at, inspection_required) VALUES (?, ?, ?, 1)",
        (DEPLOY_ID, "2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z"),
    )
    await store._db.commit()

    async with store._mutation_lock:
        await initialize_attempt_schema(store._db)
        await store._db.commit()

    assert await store.record_startup_inspection_notices() == 1
    rows = await notice_rows(store)
    assert rows[0]["sm_status"] == "retry" and rows[0]["event"] == "startup_backfill"


# ---------------------------------------------------------------- 본문


def test_redacts_secret_shapes():
    text = (
        "Authorization: Bearer bearervalue789 token=tokenvalue456 key=keyvalue123 "
        "password=pwvalue secret=secretvalue "
        "https://user:pass@example.com/x ghp_abcdef gho_abcdef github_pat_abc sk-live-123"
    )

    redacted = redact_secrets(text)

    for raw in (
        "bearervalue789",
        "tokenvalue456",
        "keyvalue123",
        "pwvalue",
        "secretvalue",
        "user:pass",
        "ghp_abcdef",
        "gho_abcdef",
        "github_pat_abc",
        "sk-live-123",
    ):
        assert raw not in redacted
    assert "Bearer ***" in redacted
    assert "https://***@example.com/x" in redacted


def test_redacts_configured_literal():
    assert "s3cr3t-value" not in redact_secrets(
        "value s3cr3t-value here", extra=("s3cr3t-value",)
    )


def test_render_lists_fields_and_uses_unknown_for_missing():
    text = render_notice_text(
        node_id="n",
        repo=None,
        branch="main",
        target_head="abc",
        deploy_id="d",
        notice_id="i",
        sm_status="retry",
        event="attempt_failed",
        failure=None,
        prior_notice_count=2,
    )

    assert "repo: 미확인" in text
    assert "last_failure kind: 미확인" in text
    assert "attempt_id: 미확인" in text
    assert "잠금을 켠 사건 이름: attempt_failed" in text
    assert "같은 요청이 전에 알림을 받은 횟수: 2" in text


def test_render_truncates_error_to_1500_chars():
    text = render_notice_text(
        node_id="n",
        repo="r",
        branch="main",
        target_head="abc",
        deploy_id="d",
        notice_id="i",
        sm_status="retry",
        event="attempt_failed",
        failure={"error": "x" * 2000},
        prior_notice_count=0,
    )

    error_line = next(line for line in text.splitlines() if line.startswith("error: "))
    assert len(error_line) == len("error: ") + 1500


async def test_notice_text_snapshots_the_last_failure_with_redaction(
    store: EventStore, monkeypatch
):
    async def fake_last_failure(db, deploy_id):
        return {
            "kind": "orchestrator_attempt",
            "attempt_id": "a1",
            "reason": "token=reastoken",
            "error": "Bearer bearer-secret " + "e" * 2000,
            "at": "2026-08-01T00:00:00Z",
        }

    monkeypatch.setattr(inspection_notice, "last_failure", fake_last_failure)
    await new_request(store)

    await fire_locking_event(store, "attempt_failed")

    row = (await notice_rows(store))[0]
    assert row["failure_kind"] == "orchestrator_attempt"
    assert row["attempt_id"] == "a1"
    assert row["reason"] == "token=***"
    assert "reastoken" not in row["text"]
    assert "bearer-secret" not in row["text"]
    assert "Bearer ***" in row["text"]
    error_line = next(
        line for line in row["text"].splitlines() if line.startswith("error: ")
    )
    assert len(error_line) == len("error: ") + 1500


async def test_notice_message_starts_with_the_instruction(store: EventStore):
    from haniel_orch.inspection_notice_sender import compose_notice_message

    await new_request(store)
    await fire_locking_event(store, "plan_failed_journal")
    row = (await notice_rows(store))[0]

    message = compose_notice_message([row["text"]])

    assert message.startswith(INSTRUCTION_TEXT)
    assert "node_id: n" in message


# ---------------------------------------------------------------- 대기 목록


async def test_pending_list_carries_the_inspection_notice(store: EventStore):
    await new_request(store, "n:r:main:locked")
    await new_request(store, "n:r:dev:clean", branch="dev")
    await fire_locking_event(store, "plan_failed_journal", "n:r:main:locked")

    hub = WebSocketHub(NodeRegistry(store), store, token="t")
    client = TestClient(Starlette(routes=create_api_routes(hub, store)))

    deploys = {item["deploy_id"]: item for item in client.get("/api/orch/pending").json()["deploys"]}

    notice = deploys["n:r:main:locked"]["inspection_notice"]
    assert set(notice) == {"notice_id", "state", "sent_at", "attempts"}
    assert notice["notice_id"].startswith("inspection-")
    assert notice["state"] == "unsent"
    assert notice["sent_at"] is None and notice["attempts"] == 0
    assert deploys["n:r:dev:clean"]["inspection_notice"] is None


async def test_pending_list_notice_is_null_when_no_ledger_row_exists(store: EventStore):
    await new_request(store)
    # 잠금은 켜졌지만 원장 줄이 없는 어긋난 상태(기동 보충 전)
    await store._db.execute(
        "UPDATE deploy_events SET sm_lock = 'l_on', sm_status = 'retry' WHERE deploy_id = ?",
        (DEPLOY_ID,),
    )
    await store._db.commit()

    hub = WebSocketHub(NodeRegistry(store), store, token="t")
    client = TestClient(Starlette(routes=create_api_routes(hub, store)))

    deploy = client.get("/api/orch/pending").json()["deploys"][0]
    assert deploy["inspection_required"] is True
    assert deploy["inspection_notice"] is None
