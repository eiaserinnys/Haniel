"""한 걸음 시험: 전이 없는 사건은 거절되고 줄에 쓰이지 않는다. 잎과 바깥 칸은 함께 간다(카드 #1259)."""

import itertools
import sqlite3

import pytest
from state_support import fire, set_legacy_status, to_deploying
from behavior_scenarios import SCENARIOS

from haniel_orch.event_store import EventStore
from haniel_orch.statechart import attempt_steps, backfill, steps
from haniel_orch.statechart.columns import (
    ATTEMPT_OUTCOMES,
    attempt_leaf_from_columns,
    attempt_leaf_is_consistent,
    request_leaf_from_columns,
    request_leaf_is_consistent,
)
from haniel_orch.statechart.definitions import (
    REQUEST_OUTER_STATUS,
    REQUIRED_FACTS,
    AttemptEvidence,
    OrchRequest,
)
from haniel_orch.statechart.docs import _region_of, transitions
from haniel_orch.statechart.restore import NoTransition, advance, initial_row, leaf_ids, regions


async def _create(store: EventStore, deploy_id: str, **kwargs) -> None:
    await store.create_deploy_event(
        deploy_id=deploy_id,
        node_id=kwargs.pop("node_id", "n"),
        repo=kwargs.pop("repo", "r"),
        branch=kwargs.pop("branch", "main"),
        commits=["c"],
        affected_services=[],
        diff_stat=None,
        detected_at="2026-08-01T00:00:00Z",
        **kwargs,
    )


async def _raw(store: EventStore, sql: str, *params):
    cursor = await store._db.execute(sql, params)
    return await cursor.fetchall()


async def _leaf(store: EventStore, deploy_id: str):
    return (
        await _raw(store, "SELECT status, sm_status FROM deploy_events WHERE deploy_id = ?", deploy_id)
    )[0]


async def _full_snapshot(store: EventStore):
    return {
        table: await _raw(store, f"SELECT * FROM {table} ORDER BY 1, 2")
        for table in (
            "deploy_events",
            "deploy_attempts",
            "deploy_retry_requirements",
            "deploy_retry_source_attempts",
        )
    }


@pytest.fixture
async def at_each_leaf(store: EventStore):
    """요청 줄을 정의의 잎마다 하나씩 실제 사건으로 세운다."""
    await _create(store, "pending", repo="r1")
    await _create(store, "retry", repo="r2")
    await store.attempts.begin_normal_attempt(
        orchestrator_attempt_id="a-retry",
        deploy_id="retry",
        connection_generation="g",
        current_generation="g",
        source="manual_single",
        approved_by="d",
        deadline_at="2099-01-01T00:00:00+00:00",
    )
    await store.attempts.fail_active_attempt(
        "a-retry", kind="k", stage="s", reason="r", error="e"
    )
    await _create(store, "deploying", repo="r3")
    await to_deploying(store, "deploying")
    await _create(store, "success", repo="r4")
    await fire(store, "success", "node_succeeded", data={"duration_ms": 1})
    await _create(store, "rejected", repo="r5")
    await store.reject_deploy("rejected", "no")
    await _create(store, "superseded", repo="r6")
    await fire(store, "superseded", "newer_commit", data={"reason": "superseded by x"})
    # 다시 대기(2b-2): 판 1 시도의 보내기 실패는 점검 필요를 세우지 않는다
    await _create(store, "again", repo="r7")
    await store.attempts.begin_normal_attempt(
        orchestrator_attempt_id="a-again",
        deploy_id="again",
        connection_generation="g",
        current_generation="g",
        source="manual_single",
        approved_by="d",
        deadline_at="2099-01-01T00:00:00+00:00",
        node_protocol=1,
    )
    await store.attempts.fail_attempt_not_sent("a-again")
    return store


def _leaves_of(cls, region):
    return [leaf for leaf in leaf_ids(cls) if _region_of(cls, leaf) == region]


async def test_every_leaf_is_reachable_through_real_events(at_each_leaf):
    store = at_each_leaf
    leaves = {row[0] for row in await _raw(store, "SELECT sm_status FROM deploy_events")}
    assert leaves == set(_leaves_of(OrchRequest, "status"))
    # 점검 필요 영역: 실패한 시도가 켠 줄(retry)과 켜지 않은 줄이 모두 있다
    assert {row[0] for row in await _raw(store, "SELECT sm_lock FROM deploy_events")} == set(
        _leaves_of(OrchRequest, "lock")
    )
    for status, leaf in await _raw(store, "SELECT status, sm_status FROM deploy_events"):
        assert REQUEST_OUTER_STATUS[leaf] == status


# at_each_leaf가 세운 줄의 점검 필요 잎. 실패한 시도가 retry에 켜 두었고 나머지는 꺼져 있다. 꼬리표는 모두 없음.
# 믿음은 배포 중(시도 줄 없이 begin_attempt만 넣음)이 닿았는지 모름이고 나머지는 도는 시도 없음
FIXTURE_LOCK = {"retry": "l_on"}
FIXTURE_BELIEF = {"deploying": "b_unknown"}
FIXTURE_TAG = "t_off"


def _no_transition_cases():
    """줄(요청 상태 잎, 믿음 잎, 점검 필요 잎, 꼬리표 잎)에서 네 영역 어디에도 그 사건의 화살표가 없는 짝."""
    cls = OrchRequest
    allowed = {(a.source, a.event) for a in transitions(cls)}
    for leaf in _leaves_of(cls, "status"):
        lock = FIXTURE_LOCK.get(leaf, "l_off")
        belief = FIXTURE_BELIEF.get(leaf, "b_none")
        for event in REQUIRED_FACTS[cls]:
            if (
                (leaf, event) not in allowed
                and (lock, event) not in allowed
                and (belief, event) not in allowed
                and (FIXTURE_TAG, event) not in allowed
            ):
                yield leaf, event


@pytest.mark.parametrize(("leaf", "event"), list(_no_transition_cases()))
async def test_event_without_a_transition_is_rejected_and_writes_nothing(
    at_each_leaf, leaf, event
):
    store = at_each_leaf
    before = await _full_snapshot(store)
    facts = {name: True for name in REQUIRED_FACTS[OrchRequest][event]}
    with pytest.raises(NoTransition):
        await steps.step_request(
            store._db, leaf, event, facts=facts, data={"approved_by": "x", "reason": "r", "duration_ms": 1}
        )
    assert await _full_snapshot(store) == before


async def test_guard_failure_is_also_no_transition_and_writes_nothing(at_each_leaf):
    store = at_each_leaf
    before = await _full_snapshot(store)
    with pytest.raises(NoTransition):
        await steps.step_request(
            store._db,
            "pending",
            "observed",
            facts={"heads_equal": True, "no_active_attempt": False, "no_failed_node_attempt": True},
        )
    assert await _full_snapshot(store) == before


async def test_unknown_row_and_legacy_row_are_no_transition(store: EventStore):
    with pytest.raises(NoTransition):
        await steps.step_request(store._db, "nope", "reject")
    await _create(store, "legacy")
    await set_legacy_status(store, "legacy", "failed")
    before = await _full_snapshot(store)
    with pytest.raises(NoTransition):
        await steps.step_request(store._db, "legacy", "reject")
    assert await _full_snapshot(store) == before


def test_introspected_transitions_equal_what_advance_allows():
    """생성기가 읽는 전이 목록과 실제로 일어나는 전이(모든 사실 조합)가 같다.

    제자리 전이(출발과 도착이 같음, 2b-2의 auto_request, link_down, newer_commit, acked, started, hello_holds, 증거의 dup)는
    줄만으로는 움직임이 보이지 않으므로 「그 잎에서 그 사건이 받아들여진다」로 따로 본다.
    """
    for cls in (OrchRequest, AttemptEvidence):
        listed = {(a.source, a.event, a.target) for a in transitions(cls) if a.source != a.target}
        self_arrows = {(a.source, a.event) for a in transitions(cls) if a.source == a.target}
        names_of = list(regions(cls))
        combos = list(itertools.product(*(_leaves_of(cls, region) for region in names_of)))
        actual = set()
        accepted = set()
        for combo in combos:
            row = dict(zip(names_of, combo))
            for event, names in REQUIRED_FACTS[cls].items():
                for values in itertools.product([False, True], repeat=len(names)):
                    try:
                        after = advance(cls, row, event, dict(zip(names, values)))
                    except NoTransition:
                        continue
                    for region in names_of:
                        if after[region] != row[region]:
                            actual.add((row[region], event, after[region]))
                        else:
                            accepted.add((row[region], event))
        assert actual == listed, cls.__name__
        assert self_arrows <= accepted, cls.__name__


async def test_marker_and_lineage_follow_the_failure_and_the_closing(store: EventStore):
    await _create(store, "n:r:main:t")
    await store.attempts.begin_normal_attempt(
        orchestrator_attempt_id="a1",
        deploy_id="n:r:main:t",
        connection_generation="g",
        current_generation="g",
        source="manual_single",
        approved_by="d",
        deadline_at="2099-01-01T00:00:00+00:00",
    )
    assert (await _leaf(store, "n:r:main:t")) == ("deploying", "deploying")
    await store.attempts.fail_active_attempt("a1", kind="k", stage="s", reason="r", error="e")
    assert (await _leaf(store, "n:r:main:t")) == ("pending", "retry")
    assert await _raw(store, "SELECT deploy_id FROM deploy_retry_requirements") == [("n:r:main:t",)]
    assert await _raw(store, "SELECT lineage_orchestrator_attempt_id FROM deploy_retry_source_attempts") == [("a1",)]
    await store.reject_deploy("n:r:main:t", "operator")
    assert (await _leaf(store, "n:r:main:t")) == ("rejected", "rejected")
    assert await _raw(store, "SELECT 1 FROM deploy_retry_requirements") == []
    assert await _raw(store, "SELECT 1 FROM deploy_retry_source_attempts") == []


async def test_attempt_leaf_and_outcome_move_together(store: EventStore):
    await _create(store, "n:r:main:t")
    await store.attempts.begin_normal_attempt(
        orchestrator_attempt_id="a1",
        deploy_id="n:r:main:t",
        connection_generation="g",
        current_generation="g",
        source="manual_single",
        approved_by="d",
        deadline_at="2099-01-01T00:00:00+00:00",
    )

    async def state():
        return (await _raw(store, "SELECT outcome, sm_evidence FROM deploy_attempts"))[0]

    assert await state() == ("active", "ev_none")
    await store.attempts.record_result(
        deploy_id="n:r:main:t", orchestrator_attempt_id="a1", connection_generation="g",
        status="success", error=None, duration_ms=1,
    )
    assert await state() == ("active", "ev_result")
    await store.attempts.record_settled(
        deploy_id="n:r:main:t", orchestrator_attempt_id="a1", connection_generation="g",
        local_head="t", remote_head="t",
    )
    assert await state() == ("success", "ev_both")
    # 끝난 시도에 같은 글이 또 오면 아무 칸도 쓰지 않고 dup 기호만 돌려준다(2b-2, design.md:81)
    step = await attempt_steps.step_attempt(store._db, "a1", "fail", data={"kind": "k", "stage": "s", "reason": "r", "error": "e"})
    assert (step.previous, step.leaf, step.todo) == ("ev_both", "ev_both", ("dup",))
    assert await state() == ("success", "ev_both")
    assert (await _raw(store, "SELECT terminal_kind FROM deploy_attempts"))[0] == (None,)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_leaves_stay_consistent_with_outer_columns_after_every_scenario(name):
    store = EventStore(":memory:")
    await store.initialize()
    try:
        await SCENARIOS[name](store)
        for _id, status, reason, leaf, marked in await _raw(
            store,
            "SELECT e.deploy_id, e.status, e.reject_reason, e.sm_status, "
            "EXISTS(SELECT 1 FROM deploy_retry_requirements r WHERE r.deploy_id = e.deploy_id) "
            "FROM deploy_events e",
        ):
            if status in ("failed", "approved"):
                # 정의에 잎이 없는 옛 값은 잎을 비워 둔다(s17)
                assert leaf is None, (_id, status, leaf)
                continue
            assert request_leaf_is_consistent(leaf, status, bool(marked)), (_id, status, leaf, marked)
            assert request_leaf_from_columns(status, reason, bool(marked)) == leaf or leaf == "rejected"
        for _id, outcome, result_status, settled_at, leaf in await _raw(
            store,
            "SELECT orchestrator_attempt_id, outcome, result_status, settled_at, sm_evidence "
            "FROM deploy_attempts",
        ):
            assert attempt_leaf_is_consistent(leaf, outcome), (_id, outcome, leaf)
            if outcome == "active":
                assert attempt_leaf_from_columns(outcome, result_status, settled_at) == leaf
            else:
                # 닫힌 시도의 증거 잎은 닫힘과 따로 움직인다(2b-1). 스스로 닫은 시도는 증거 잎이 열린 채 남는다
                assert attempt_leaf_from_columns(outcome, result_status, settled_at) in {leaf, "ev_failed"}
    finally:
        await store.close()


def test_outcome_values_cover_every_attempt_leaf():
    assert set(ATTEMPT_OUTCOMES) == set(leaf_ids(AttemptEvidence))


async def _seed_old_db(path):
    """잎 칸이 없는 옛 DB를 만든다(옛 코드가 바깥 칸만 쓴 줄들)."""
    seed = EventStore(str(path))
    await seed.initialize()
    await _create(seed, "p", repo="r1")
    await _create(seed, "m", repo="r2")
    await _create(seed, "d", repo="r3")
    await _create(seed, "s", repo="r4")
    await _create(seed, "rj", repo="r5")
    await _create(seed, "sup", repo="r6")
    await _create(seed, "f", repo="r7")
    await seed.close()
    con = sqlite3.connect(path)
    con.execute("UPDATE deploy_events SET status='deploying' WHERE deploy_id='d'")
    con.execute("UPDATE deploy_events SET status='success' WHERE deploy_id='s'")
    con.execute("UPDATE deploy_events SET status='rejected', reject_reason='operator' WHERE deploy_id='rj'")
    con.execute("UPDATE deploy_events SET status='rejected', reject_reason='superseded by x' WHERE deploy_id='sup'")
    con.execute("UPDATE deploy_events SET status='failed' WHERE deploy_id='f'")
    con.execute(
        "INSERT INTO deploy_retry_requirements "
        "(deploy_id, source_orchestrator_attempt_id, last_failed_orchestrator_attempt_id, created_at, updated_at) "
        "VALUES ('m','a','a','2026-08-01T00:00:00Z','2026-08-01T00:00:00Z')"
    )
    con.execute("UPDATE deploy_events SET sm_status = NULL, sm_lock = NULL")
    for attempt_id, outcome, result, settled in (
        ("act-none", "active", None, None),
        ("act-result", "active", "success", None),
        ("act-settled", "active", None, "2026-08-01T00:00:00Z"),
        ("ok", "success", "success", "2026-08-01T00:00:00Z"),
        ("rec", "evidence_recovered_success", None, None),
        ("bad", "failed", None, None),
        ("sup", "superseded", None, None),
    ):
        con.execute(
            "INSERT INTO deploy_attempts (orchestrator_attempt_id, deploy_id, node_id, repo, branch, "
            "target_head, source, execution_mode, approved_by, connection_generation, commits_json, "
            "affected_services_json, detected_at, started_at, deadline_at, outcome, result_status, "
            "settled_at, sm_evidence) VALUES (?, ?, 'n', ?, 'main', 't', 's', 'execute', 'd', 'g', "
            "'[]', '[]', 'z', 'z', 'z', ?, ?, ?, NULL)",
            (attempt_id, attempt_id, attempt_id, outcome, result, settled),
        )
    con.commit()
    con.close()


async def test_startup_recomputes_empty_leaves_from_the_old_columns(tmp_path):
    path = tmp_path / "old.sqlite3"
    await _seed_old_db(path)

    store = EventStore(str(path))
    await store.initialize()
    try:
        leaves = dict(await _raw(store, "SELECT deploy_id, sm_status FROM deploy_events"))
        assert leaves == {
            "p": "pending",
            "m": "retry",
            "d": "deploying",
            "s": "success",
            "rj": "rejected",
            "sup": "superseded",
            "f": None,  # 정의에 잎이 없는 옛 값
        }
        # 점검 필요 잎은 표시 줄이 있는 줄(m)만 켜진다. 정의에 잎이 없는 옛 값 줄도 잎이 채워진다
        assert dict(await _raw(store, "SELECT deploy_id, sm_lock FROM deploy_events")) == {
            "p": "l_off",
            "m": "l_on",
            "d": "l_off",
            "s": "l_off",
            "rj": "l_off",
            "sup": "l_off",
            "f": "l_off",
        }
        evidence = dict(await _raw(store, "SELECT orchestrator_attempt_id, sm_evidence FROM deploy_attempts"))
        assert evidence == {
            "act-none": "ev_none",
            "act-result": "ev_result",
            "act-settled": "ev_settled",
            "ok": "ev_both",
            "rec": "ev_both",
            "bad": "ev_failed",
            "sup": "ev_superseded",
        }
    finally:
        await store.close()


async def test_startup_fixes_a_leaf_that_disagrees_with_the_outer_column(tmp_path):
    """옛 코드로 되돌렸다 다시 올린 경우: 바깥 칸만 바뀐 줄을 맞춘다. 맞는 줄은 건드리지 않는다."""
    path = tmp_path / "rollback.sqlite3"
    first = EventStore(str(path))
    await first.initialize()
    await _create(first, "a", repo="r1")
    await _create(first, "b", repo="r2")
    await to_deploying(first, "b")
    await first.close()
    con = sqlite3.connect(path)
    con.execute("UPDATE deploy_events SET status='success' WHERE deploy_id='a'")  # 옛 코드가 바깥 칸만 고침
    con.commit()
    con.close()

    store = EventStore(str(path))
    await store.initialize()
    try:
        assert dict(await _raw(store, "SELECT deploy_id, sm_status FROM deploy_events")) == {
            "a": "success",
            "b": "deploying",
        }
        fixed = await backfill.backfill_leaves(store._db)
        assert fixed == {"requests": 0, "locks": 0, "beliefs": 0, "tags": 0, "attempts": 0, "attempt_seqs": 0}
    finally:
        await store.close()


async def test_leaves_survive_a_restart_and_events_continue(tmp_path):
    path = tmp_path / "restart.sqlite3"
    first = EventStore(str(path))
    await first.initialize()
    await _create(first, "n:r:main:t")
    await first.attempts.begin_normal_attempt(
        orchestrator_attempt_id="a1",
        deploy_id="n:r:main:t",
        connection_generation="g",
        current_generation="g",
        source="manual_single",
        approved_by="d",
        deadline_at="2099-01-01T00:00:00+00:00",
    )
    await first.close()

    second = EventStore(str(path))
    await second.initialize()
    try:
        result = await second.attempts.record_settled(
            deploy_id="n:r:main:t", orchestrator_attempt_id="a1", connection_generation="g",
            local_head="t", remote_head="t",
        )
        assert result["status"] == "recorded"
        assert (await _leaf(second, "n:r:main:t")) == ("deploying", "deploying")
        assert (await _raw(second, "SELECT sm_evidence FROM deploy_attempts"))[0][0] == "ev_settled"
    finally:
        await second.close()
