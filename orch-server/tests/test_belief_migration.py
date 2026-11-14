"""2b-2 이전 시험: 2b-1 판 스키마의 DB에 믿음 잎과 새 칸을 더한다(카드 #1259 PR 2b-2, 18항).

새 칸(deploy_events.sm_belief, deploy_attempts.node_protocol, deadline_kind, closure, nodes.deploy_protocol)은 모두 NULL 허용이라
2b-1 코드로 되돌려도 삽입이 깨지지 않는다. 믿음 잎은 NULL인 줄만 채운다: 도는 시도가 없으면 도는 시도 없음, 있으면 실행 시작
시각이 있으면 시작했다, 없으면 닿았는지 모름. 두 번 돌려도 같다. 표시 줄 있는 대기는 inspection_required가 1이면 retry, 0이면 again이다.
"""

import sqlite3

from haniel_orch.deploy_attempt_schema import initialize_attempt_schema
from haniel_orch.event_store import EventStore

NEW_EVENT_COLUMNS = ("sm_belief",)
NEW_ATTEMPT_COLUMNS = ("node_protocol", "deadline_kind", "closure")

ATTEMPT_INSERT_2B1 = (
    "INSERT INTO deploy_attempts (orchestrator_attempt_id, deploy_id, node_id, repo, branch, target_head, source, "
    "execution_mode, approved_by, connection_generation, commits_json, affected_services_json, detected_at, started_at, "
    "deadline_at, outcome, sm_evidence, attempt_seq, execution_started_at) "
    "VALUES (?, ?, 'n', ?, 'main', 't', 'manual_single', 'execute', 'd', 'g', '[]', '[]', 'z', ?, '2099-01-01T00:00:00+00:00', ?, ?, ?, ?)"
)
EVENT_INSERT_2B1 = (
    "INSERT INTO deploy_events (deploy_id, node_id, repo, branch, status, sm_status, sm_lock, sm_tag, commits_json, "
    "affected_services_json, detected_at, created_at, updated_at, target_head) "
    "VALUES (?, 'n', ?, 'main', ?, ?, ?, 't_off', '[]', '[]', ?, 'z', 'z', 't')"
)


def one(path, sql):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


async def make_2b1_db(path) -> None:
    """2b-1 판 스키마의 DB: 새 칸이 없다. 줄은 2b-1 코드가 남긴 모양으로 직접 넣는다."""
    store = EventStore(str(path))
    await store.initialize()
    await store.close()
    con = sqlite3.connect(path)
    for column in NEW_EVENT_COLUMNS:
        con.execute(f"ALTER TABLE deploy_events DROP COLUMN {column}")
    for column in NEW_ATTEMPT_COLUMNS:
        con.execute(f"ALTER TABLE deploy_attempts DROP COLUMN {column}")
    con.execute("ALTER TABLE nodes DROP COLUMN deploy_protocol")
    rows = [
        # deploy_id, repo, status, sm_status, sm_lock, detected_at
        ("n:r1:main:idle", "r1", "pending", "pending", "l_off", "2026-08-01T00:00:00Z"),
        ("n:r2:main:unknown", "r2", "deploying", "deploying", "l_off", "2026-08-02T00:00:00Z"),
        ("n:r3:main:running", "r3", "deploying", "deploying", "l_off", "2026-08-03T00:00:00Z"),
        ("n:r4:main:closed", "r4", "success", "success", "l_off", "2026-08-04T00:00:00Z"),
        ("n:r5:main:retry", "r5", "pending", "retry", "l_on", "2026-08-05T00:00:00Z"),
        ("n:r6:main:again", "r6", "pending", "again", "l_off", "2026-08-06T00:00:00Z"),
        ("n:r7:main:rollback", "r7", "pending", "retry", "l_off", "2026-08-07T00:00:00Z"),
    ]
    for row in rows:
        con.execute(EVENT_INSERT_2B1, row)
    con.execute(ATTEMPT_INSERT_2B1, ("a-unknown", "n:r2:main:unknown", "r2", "2026-08-02T00:01:00Z", "active", "ev_none", 1, None))
    con.execute(ATTEMPT_INSERT_2B1, ("a-running", "n:r3:main:running", "r3", "2026-08-03T00:01:00Z", "active", "ev_none", 1, "2026-08-03T00:02:00Z"))
    con.execute(ATTEMPT_INSERT_2B1, ("a-closed", "n:r4:main:closed", "r4", "2026-08-04T00:01:00Z", "success", "ev_both", 1, "2026-08-04T00:02:00Z"))
    for deploy_id, inspection in (("n:r5:main:retry", 1), ("n:r6:main:again", 0), ("n:r7:main:rollback", 1)):
        con.execute(
            "INSERT INTO deploy_retry_requirements (deploy_id, source_orchestrator_attempt_id, last_failed_orchestrator_attempt_id, "
            "created_at, updated_at, inspection_required) VALUES (?, 'x', 'x', 'z', 'z', ?)",
            (deploy_id, inspection),
        )
    con.commit()
    con.close()


async def migrate(path) -> dict:
    store = EventStore(str(path))
    await store.initialize()
    await store.close()
    return {
        "beliefs": dict(one(path, "SELECT deploy_id, sm_belief FROM deploy_events")),
        "leaves": dict(one(path, "SELECT deploy_id, sm_status || '/' || sm_lock FROM deploy_events")),
        "attempts": one(path, "SELECT orchestrator_attempt_id, node_protocol, deadline_kind, closure FROM deploy_attempts ORDER BY 1"),
    }


async def test_migration_adds_nullable_columns_and_fills_only_the_belief(tmp_path):
    path = tmp_path / "2b1.sqlite3"
    await make_2b1_db(path)
    first = await migrate(path)
    assert first["beliefs"] == {
        "n:r1:main:idle": "b_none",
        "n:r2:main:unknown": "b_unknown",
        "n:r3:main:running": "b_running",
        "n:r4:main:closed": "b_none",
        "n:r5:main:retry": "b_none",
        "n:r6:main:again": "b_none",
        "n:r7:main:rollback": "b_none",
    }
    # 새 시도 칸은 NULL 그대로다(NULL deadline_kind는 실행 임대로 읽는다)
    assert first["attempts"] == [("a-closed", None, None, None), ("a-running", None, None, None), ("a-unknown", None, None, None)]
    columns = {row[1]: row[3] for row in one(path, "PRAGMA table_info(deploy_attempts)")}
    assert all(columns[c] == 0 for c in NEW_ATTEMPT_COLUMNS)  # NOT NULL이 아니다
    assert {row[1] for row in one(path, "PRAGMA table_info(nodes)")} >= {"deploy_protocol"}
    assert one(path, "SELECT count(*) FROM deploy_events") == [(7,)]


async def test_marked_waits_follow_inspection_required_and_the_rollback_repair_stays_one_way(tmp_path):
    path = tmp_path / "2b1.sqlite3"
    await make_2b1_db(path)
    leaves = (await migrate(path))["leaves"]
    assert leaves["n:r5:main:retry"] == "retry/l_on"
    assert leaves["n:r6:main:again"] == "again/l_off"  # 2b-2의 다시 대기는 그대로 둔다
    assert leaves["n:r7:main:rollback"] == "retry/l_on"  # PR 1 되돌림 줄: 표시 줄의 inspection_required 1을 따라 켠다


async def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "2b1.sqlite3"
    await make_2b1_db(path)
    first = await migrate(path)
    second = await migrate(path)
    assert first == second


async def test_a_2b1_style_insert_still_works_after_the_migration_and_the_next_start_fills_the_belief(tmp_path):
    path = tmp_path / "2b1.sqlite3"
    await make_2b1_db(path)
    await migrate(path)
    con = sqlite3.connect(path)
    con.execute(EVENT_INSERT_2B1, ("n:r9:main:new", "r9", "deploying", "deploying", "l_off", "2026-08-09T00:00:00Z"))
    con.execute(ATTEMPT_INSERT_2B1, ("a-new", "n:r9:main:new", "r9", "2026-08-09T00:01:00Z", "active", "ev_none", 1, None))
    con.commit()
    con.close()
    assert one(path, "SELECT sm_belief FROM deploy_events WHERE deploy_id = 'n:r9:main:new'") == [(None,)]
    migrated = await migrate(path)
    assert migrated["beliefs"]["n:r9:main:new"] == "b_unknown"


async def test_backfill_report_counts_beliefs(tmp_path):
    from haniel_orch.statechart.backfill import backfill_leaves
    import aiosqlite

    path = tmp_path / "2b1.sqlite3"
    await make_2b1_db(path)
    async with aiosqlite.connect(str(path)) as db:
        await initialize_attempt_schema(db)
        await db.commit()
        again = await backfill_leaves(db)
    assert again == {"requests": 0, "locks": 0, "beliefs": 0, "tags": 0, "attempts": 0, "attempt_seqs": 0}
