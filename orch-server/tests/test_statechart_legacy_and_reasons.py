"""옛 모양의 줄이 든 DB, 전이 없음의 사유, 닫힌 요청에 닿은 시도 실패(카드 #1259).

운영 DB 읽기 전용 사본(deploy_events 9,236줄 가운데 status가 failed인 119줄, approved인 3줄,
시도 짝 없음, 표시 줄과 계보 줄 없음)과 같은 모양을 시험 fixture로 만든다. 기준 커밋(155b69f)과
같은 답은 `test_behavior_baseline.py`의 s17_legacy_rows와 rest_legacy가 견준다.
"""

import logging

import pytest

from behavior_scenarios import LEGACY_ROWS, _create, _insert_legacy_rows
from haniel_orch.deploy_attempt_schema import initialize_attempt_schema
from haniel_orch.event_store import EventStore
from haniel_orch.statechart import steps
from haniel_orch.statechart.definitions import OrchRequest
from haniel_orch.statechart.restore import (
    REASON_NO_ROW,
    REASON_NO_TRANSITION,
    REASON_STATE_MISMATCH,
    REASON_UPDATE_ZERO,
    NoTransition,
    advance,
)

# 옛 줄이 기동 때 얻는 잎. failed와 approved는 정의에 잎이 없어 비워 둔다
EXPECTED_LEAVES = {
    "n:r1:main:lf1": None,
    "n:r2:main:lf2": None,
    "n:r3:main:la1": None,
    "n:r4:main:lr0": "rejected",  # 사유 NULL
    "n:r5:main:lr1": "rejected",  # 사유가 superseded by가 아님
    "n:r6:main:lr2": "superseded",
    "n:r7:main:lr3": "rejected",
    "n:r8:main:lr4": "superseded",
}


@pytest.fixture
async def legacy_store(store: EventStore):
    await _create(store, "n:live:main:p1", repo="live", detected="2026-08-10T00:00:00Z")
    await _insert_legacy_rows(store)
    return store


async def _leaves(store: EventStore) -> dict:
    cursor = await store._db.execute("SELECT deploy_id, sm_status FROM deploy_events")
    return dict(await cursor.fetchall())


async def test_startup_leaves_of_legacy_rows_are_fixed(legacy_store: EventStore):
    leaves = await _leaves(legacy_store)
    assert {k: leaves[k] for k in EXPECTED_LEAVES} == EXPECTED_LEAVES
    assert leaves["n:live:main:p1"] == "pending"
    # 한 번 더 기동해도 같다
    await initialize_attempt_schema(legacy_store._db)
    assert await _leaves(legacy_store) == leaves


async def test_reject_and_node_reports_on_legacy_rows_never_raise(legacy_store, caplog):
    caplog.set_level(logging.WARNING, logger="haniel_orch.statechart")
    results = {}
    for deploy_id in EXPECTED_LEAVES:
        results[deploy_id] = await legacy_store.reject_deploy(deploy_id, "operator")
    # 거절은 처음 대기와 표시 줄 있는 대기에서만 된다(어긋남 8이 없어졌다). 닫힌 줄은 전이 없음으로 False다.
    # 잎이 없는 옛 값은 줄을 건드리지 않고 False다. REST는 이 줄에 거절을 아예 보내지 않는다(409)
    assert all(ok is False for ok in results.values())
    cursor = await legacy_store._db.execute(
        "SELECT deploy_id, status FROM deploy_events WHERE deploy_id IN (?, ?, ?)",
        ("n:r1:main:lf1", "n:r2:main:lf2", "n:r3:main:la1"),
    )
    assert sorted(await cursor.fetchall()) == [
        ("n:r1:main:lf1", "failed"),
        ("n:r2:main:lf2", "failed"),
        ("n:r3:main:la1", "approved"),
    ]
    warned = [r.getMessage() for r in caplog.records if "전이 없음을 넘김" in r.getMessage()]
    assert len(warned) == 8
    assert all("자리=reject" in m for m in warned)
    assert sum(f"사유={REASON_STATE_MISMATCH}" in m for m in warned) == 3
    assert sum(f"사유={REASON_NO_TRANSITION}" in m for m in warned) == 5
    assert any("n:r3:main:la1" in m for m in warned)


async def test_a_legacy_row_with_a_leaf_is_reopened_like_the_baseline(legacy_store):
    """바깥 status가 rejected인 자기 갱신 보류 줄은 사유 접두와 상관없이 되살아난다(기준 동작)."""
    for deploy_id in ("n:r7:main:lr3", "n:r8:main:lr4"):
        assert await legacy_store.reopen_postponed_self_update(deploy_id) is True
    leaves = await _leaves(legacy_store)
    assert leaves["n:r7:main:lr3"] == leaves["n:r8:main:lr4"] == "pending"


async def test_no_transition_names_why(store: EventStore, caplog):
    caplog.set_level(logging.WARNING, logger="haniel_orch.statechart")
    await _create(store, "d1")

    with pytest.raises(NoTransition) as exc:
        await steps.step_request(store._db, "missing", "reject")
    assert (exc.value.reason, exc.value.key) == (REASON_NO_ROW, "missing")

    await store._db.execute("UPDATE deploy_events SET sm_status = 'rejected' WHERE deploy_id = 'd1'")
    with pytest.raises(NoTransition) as exc:
        await steps.step_request(store._db, "d1", "reject")
    assert (exc.value.reason, exc.value.key) == (REASON_STATE_MISMATCH, "d1")
    await store._db.execute("UPDATE deploy_events SET sm_status = 'pending' WHERE deploy_id = 'd1'")

    with pytest.raises(NoTransition) as exc:
        await steps.step_request(store._db, "d1", "attempt_succeeded")
    assert (exc.value.reason, exc.value.key) == (REASON_NO_TRANSITION, "d1")
    exc.value.warn("test")
    assert any(
        "자리=test" in r.getMessage() and f"사유={REASON_NO_TRANSITION}" in r.getMessage() and "id=d1" in r.getMessage()
        for r in caplog.records
    )


async def test_a_zero_row_update_is_named_update_zero(store: EventStore):
    await _create(store, "d1")

    class ZeroUpdate:
        def __init__(self, db):
            self.db = db

        async def execute(self, sql, params=()):
            if sql.lstrip().startswith("UPDATE deploy_events"):

                class Done:
                    rowcount = 0

                return Done()
            return await self.db.execute(sql, params)

    with pytest.raises(NoTransition) as exc:
        await steps.step_request(ZeroUpdate(store._db), "d1", "reject")
    assert (exc.value.reason, exc.value.key) == (REASON_UPDATE_ZERO, "d1")


@pytest.mark.parametrize("leaf", ["pending", "retry", "deploying", "success", "rejected", "superseded"])
def test_attempt_failure_reaches_every_request_leaf_with_only_the_lock_unless_deploying(leaf):
    """PR 1은 요청 상태를 보지 않고 더 새 커밋이 있으면 거절로 덮고 없으면 표시 줄을 썼다(어긋남 8).
    이제는 도는 시도가 있는 배포 중만 요청 상태를 옮기고, 그 밖의 줄에는 점검 필요만 켠다."""
    for newer in (True, False):
        row = {"status": leaf, "lock": "l_off", "tag": "t_off"}
        after = advance(OrchRequest, row, "attempt_failed", {"newer": newer})
        if leaf == "deploying":
            assert after == {"status": "superseded" if newer else "retry", "lock": "l_on", "tag": "t_off"}
        else:
            assert after == {"status": leaf, "lock": "l_on", "tag": "t_off"}
        # 점검 필요가 이미 켜져 있으면 닫힌 줄과 열린 줄 모두 전이가 없다
        locked = {"status": leaf, "lock": "l_on", "tag": "t_off"}
        if leaf == "deploying":
            assert advance(OrchRequest, locked, "attempt_failed", {"newer": newer})["lock"] == "l_on"
        else:
            with pytest.raises(NoTransition):
                advance(OrchRequest, locked, "attempt_failed", {"newer": newer})
