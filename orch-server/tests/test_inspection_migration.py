"""점검 필요 이전 시험(카드 #1259 PR 2a): PR 1 판 스키마로 만든 DB를 이 판이 이전한다.

표시 줄 표(deploy_retry_requirements)를 다시 만들어 두 시도 번호 칸의 NOT NULL을 풀고 inspection_required를 더한다.
옛 줄은 모두 inspection_required=1이 된다. 두 번 돌려도 같고, 이전한 DB를 PR 1 판의 쓰기가 그대로 쓸 수 있다.
"""

import sqlite3

import aiosqlite
import pytest

from haniel_orch import deploy_attempt_schema
from haniel_orch.event_store import EventStore

# PR 1 판(main 119016d6)의 표시 줄 표와 쓰기
PR1_MARKER_DDL = """CREATE TABLE deploy_retry_requirements (
    deploy_id TEXT PRIMARY KEY,
    source_orchestrator_attempt_id TEXT NOT NULL,
    last_failed_orchestrator_attempt_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)"""
PR1_UPSERT = """INSERT INTO deploy_retry_requirements
   (deploy_id, source_orchestrator_attempt_id, last_failed_orchestrator_attempt_id, created_at, updated_at)
   VALUES (?, ?, ?, ?, ?)
   ON CONFLICT(deploy_id) DO UPDATE SET
     source_orchestrator_attempt_id=excluded.source_orchestrator_attempt_id,
     last_failed_orchestrator_attempt_id=excluded.last_failed_orchestrator_attempt_id,
     updated_at=excluded.updated_at"""

# 되돌림: PR 1 코드로 되돌리기 전에 지울 줄(PR 본문과 같은 SQL)
ROLLBACK_SQL = (
    "DELETE FROM deploy_retry_source_attempts WHERE deploy_id IN "
    "(SELECT deploy_id FROM deploy_retry_requirements WHERE source_orchestrator_attempt_id IS NULL)",
    "DELETE FROM deploy_retry_requirements WHERE source_orchestrator_attempt_id IS NULL",
)

ROWS = {  # deploy_id -> (status, sm_status, 표시 줄 (source, last_failed) 또는 None, 계보)
    "n:r1:main:pp": ("pending", "pending", None, []),
    "n:r2:main:rt": ("pending", "retry", ("a1", "a1"), ["a1"]),
    "n:r3:main:dp": ("deploying", "deploying", ("a2", "a3"), ["a2", "a3"]),
    "n:r4:main:cl": ("success", "success", ("a4", "a4"), ["a4"]),  # 닫힌 줄에 남은 표시 줄
    "n:r5:main:rj": ("rejected", "rejected", None, []),
}
STAMP = "2026-08-01T00:00:00Z"


async def make_pr1_db(path) -> None:
    """PR 1 판 스키마의 DB를 만든다: sm_lock 칸이 없고 표시 줄 표의 두 칸이 NOT NULL이다."""
    seed = EventStore(str(path))
    await seed.initialize()
    for index, deploy_id in enumerate(ROWS, 1):
        await seed.create_deploy_event(
            deploy_id=deploy_id,
            node_id="n",
            repo=f"r{index}",
            branch="main",
            commits=["c"],
            affected_services=[],
            diff_stat=None,
            detected_at=STAMP,
            target_head=deploy_id.rsplit(":", 1)[-1],
        )
    await seed.close()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE deploy_events DROP COLUMN sm_lock")
    con.execute("DROP TABLE deploy_retry_requirements")
    con.execute(PR1_MARKER_DDL)
    for deploy_id, (status, leaf, marker, lineage) in ROWS.items():
        con.execute(
            "UPDATE deploy_events SET status = ?, sm_status = ? WHERE deploy_id = ?",
            (status, leaf, deploy_id),
        )
        if marker:
            con.execute(PR1_UPSERT, (deploy_id, *marker, STAMP, STAMP))
        for attempt in lineage:
            con.execute(
                "INSERT INTO deploy_retry_source_attempts VALUES (?, ?, ?)", (deploy_id, attempt, STAMP)
            )
    con.commit()
    con.close()


def dump(path) -> dict:
    con = sqlite3.connect(path)
    try:
        out = {
            table: con.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
            for table in (
                "deploy_events",
                "deploy_attempts",
                "deploy_retry_requirements",
                "deploy_retry_source_attempts",
            )
        }
        out["marker_columns"] = con.execute("PRAGMA table_info(deploy_retry_requirements)").fetchall()
        out["tables"] = sorted(r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'"))
        return out
    finally:
        con.close()


def one(path, sql, *params):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


async def migrate(path) -> None:
    store = EventStore(str(path))
    await store.initialize()
    await store.close()


async def test_migration_keeps_rows_and_values_and_sets_inspection_required(tmp_path):
    path = tmp_path / "pr1.sqlite3"
    await make_pr1_db(path)
    markers_before = one(path, "SELECT * FROM deploy_retry_requirements ORDER BY deploy_id")
    lineage_before = one(path, "SELECT * FROM deploy_retry_source_attempts ORDER BY 1, 2")
    events_before = one(path, "SELECT deploy_id, status, sm_status, updated_at FROM deploy_events ORDER BY 1")

    await migrate(path)

    after = one(path, "SELECT * FROM deploy_retry_requirements ORDER BY deploy_id")
    assert len(after) == len(markers_before) == 3  # 줄 수 보존
    assert [row[:5] for row in after] == markers_before  # 값 보존
    assert {row[5] for row in after} == {1}  # 옛 줄은 모두 점검 필요
    assert one(path, "SELECT * FROM deploy_retry_source_attempts ORDER BY 1, 2") == lineage_before
    assert one(path, "SELECT deploy_id, status, sm_status, updated_at FROM deploy_events ORDER BY 1") == events_before
    notnull = {row[1]: row[3] for row in one(path, "PRAGMA table_info(deploy_retry_requirements)")}
    assert notnull["source_orchestrator_attempt_id"] == 0
    assert notnull["last_failed_orchestrator_attempt_id"] == 0
    assert notnull["inspection_required"] == 1
    assert "deploy_retry_requirements_new" not in dump(path)["tables"]
    # 점검 필요 잎: 표시 줄이 있으면 켜짐(닫힌 줄과 배포 중인 줄 포함), 없으면 꺼짐
    assert dict(one(path, "SELECT deploy_id, sm_lock FROM deploy_events")) == {
        "n:r1:main:pp": "l_off",
        "n:r2:main:rt": "l_on",
        "n:r3:main:dp": "l_on",
        "n:r4:main:cl": "l_on",
        "n:r5:main:rj": "l_off",
    }


async def test_migration_twice_gives_the_same_database(tmp_path):
    path = tmp_path / "pr1.sqlite3"
    await make_pr1_db(path)
    await migrate(path)
    once = dump(path)

    await migrate(path)

    assert dump(path) == once


async def test_migrated_database_accepts_the_pr1_marker_write(tmp_path):
    path = tmp_path / "pr1.sqlite3"
    await make_pr1_db(path)
    await migrate(path)
    con = sqlite3.connect(path)
    con.execute("UPDATE deploy_retry_requirements SET inspection_required = 0 WHERE deploy_id = 'n:r2:main:rt'")
    con.commit()

    con.execute(PR1_UPSERT, ("n:r1:main:pp", "b1", "b1", STAMP, STAMP))  # 새 줄: 기본값 1
    con.execute(PR1_UPSERT, ("n:r2:main:rt", "b2", "b2", STAMP, "later"))  # 있는 줄 갱신: 새 칸은 그대로
    con.commit()

    rows = {r[0]: r[1:] for r in con.execute("SELECT * FROM deploy_retry_requirements")}
    con.close()
    assert rows["n:r1:main:pp"][-1] == 1
    assert rows["n:r2:main:rt"] == ("b2", "b2", STAMP, "later", 0)


async def test_startup_fix_turns_the_lock_on_in_one_direction_only(tmp_path):
    path = tmp_path / "pr1.sqlite3"
    await make_pr1_db(path)
    await migrate(path)
    con = sqlite3.connect(path)
    # PR 1 코드로 되돌렸다 다시 올린 줄: 대기인데 표시 줄(inspection_required는 PR 1이 모르는 칸이라 기본값 1)이 있고
    # 점검 필요 잎이 꺼진 채 남았다. 2b-2부터 inspection_required가 0인 대기는 다시 대기(again)라 고치지 않는다
    con.execute("UPDATE deploy_events SET sm_lock = 'l_off' WHERE deploy_id = 'n:r2:main:rt'")
    # 배포 중이고 표시 줄이 있는 줄은 inspection_required 값을 그대로 잎으로 읽는다. 켜짐은 내리지 않는다
    con.execute("UPDATE deploy_events SET sm_lock = 'l_off' WHERE deploy_id = 'n:r3:main:dp'")
    con.execute("UPDATE deploy_retry_requirements SET inspection_required = 0 WHERE deploy_id = 'n:r3:main:dp'")
    con.execute("UPDATE deploy_events SET sm_lock = 'l_on' WHERE deploy_id = 'n:r5:main:rj'")  # 표시 줄 없는 닫힘의 켜짐
    con.commit()
    con.close()

    await migrate(path)

    assert dict(one(path, "SELECT deploy_id, sm_lock FROM deploy_events")) == {
        "n:r1:main:pp": "l_off",
        "n:r2:main:rt": "l_on",  # 켜짐으로 고친다
        "n:r3:main:dp": "l_off",  # 배포 중은 그대로
        "n:r4:main:cl": "l_on",
        "n:r5:main:rj": "l_on",  # 그 밖의 켜짐은 내리지 않는다
    }
    assert dict(one(path, "SELECT deploy_id, inspection_required FROM deploy_retry_requirements")) == {
        "n:r2:main:rt": 1,
        "n:r3:main:dp": 0,
        "n:r4:main:cl": 1,
    }


async def test_a_deploying_row_with_an_inspection_value_zero_is_read_as_lock_off(tmp_path):
    """sm_lock이 비어 있는 배포 중 줄은 표시 줄의 inspection_required를 그대로 잎으로 읽는다."""
    path = tmp_path / "pr1.sqlite3"
    await make_pr1_db(path)
    await migrate(path)
    con = sqlite3.connect(path)
    con.execute("UPDATE deploy_retry_requirements SET inspection_required = 0 WHERE deploy_id = 'n:r3:main:dp'")
    con.execute("UPDATE deploy_events SET sm_lock = NULL")
    con.commit()
    con.close()

    await migrate(path)

    assert dict(one(path, "SELECT deploy_id, sm_lock FROM deploy_events")) == {
        "n:r1:main:pp": "l_off",
        "n:r2:main:rt": "l_on",
        "n:r3:main:dp": "l_off",
        "n:r4:main:cl": "l_on",
        "n:r5:main:rj": "l_off",
    }


async def test_a_failure_inside_the_migration_rolls_everything_back(tmp_path):
    path = tmp_path / "pr1.sqlite3"
    await make_pr1_db(path)
    before = dump(path)

    class FailAtRename:
        def __init__(self, db) -> None:
            self.db = db

        @property
        def in_transaction(self) -> bool:
            return self.db.in_transaction

        async def commit(self) -> None:
            await self.db.commit()

        async def execute(self, sql, *params):
            if "RENAME" in sql:
                raise RuntimeError("interrupted")
            return await self.db.execute(sql, *params)

    async with aiosqlite.connect(path) as db:
        with pytest.raises(RuntimeError, match="interrupted"):
            await deploy_attempt_schema._migrate_retry_requirements(FailAtRename(db))
        assert db.in_transaction is False

    assert dump(path) == before  # 옛 표와 줄이 그대로고 임시 표도 남지 않는다
    await migrate(path)  # 다시 돌리면 이전된다
    assert {row[1]: row[3] for row in dump(path)["marker_columns"]}["inspection_required"] == 1


async def test_a_stray_temporary_table_from_an_earlier_interrupted_run_does_not_block_it(tmp_path):
    path = tmp_path / "pr1.sqlite3"
    await make_pr1_db(path)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE deploy_retry_requirements_new (deploy_id TEXT)")
    con.commit()
    con.close()

    await migrate(path)

    assert "deploy_retry_requirements_new" not in dump(path)["tables"]
    assert len(one(path, "SELECT * FROM deploy_retry_requirements")) == 3


async def test_rollback_sql_selects_exactly_the_rows_pr1_cannot_read(store: EventStore):
    """PR 1 코드로 되돌리기 전에 지울 줄: source가 빈 표시 줄(스스로 한 배포의 실패, 첫 시도의 보내기 실패)."""
    stamp = STAMP
    for deploy_id, source, last in (
        ("blank", None, None),  # 노드가 스스로 한 배포의 실패
        ("unsent-first", None, "u1"),  # 첫 시도의 보내기 실패
        ("sourced", "s1", "s1"),  # PR 1도 읽는 줄
    ):
        await store._db.execute(
            "INSERT INTO deploy_retry_requirements "
            "(deploy_id, source_orchestrator_attempt_id, last_failed_orchestrator_attempt_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (deploy_id, source, last, stamp, stamp),
        )
        await store._db.execute(
            "INSERT INTO deploy_retry_source_attempts VALUES (?, ?, ?)", (deploy_id, last or "x", stamp)
        )

    for sql in ROLLBACK_SQL:
        await store._db.execute(sql)
    await store._db.commit()

    cursor = await store._db.execute("SELECT deploy_id FROM deploy_retry_requirements")
    assert [row[0] for row in await cursor.fetchall()] == ["sourced"]
    cursor = await store._db.execute("SELECT deploy_id FROM deploy_retry_source_attempts")
    assert [row[0] for row in await cursor.fetchall()] == ["sourced"]
