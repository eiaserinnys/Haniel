"""시도 순서, 실행 시작 시각, 꼬리표 칸의 이전 시험(카드 #1259 PR 2b-1): 2a 판 스키마로 만든 DB를 이 판이 이전한다.

새 칸은 모두 NULL 허용이다. 기동 때 attempt_seq가 빈 시도 줄만 요청마다 (started_at, rowid) 순으로 채우고,
sm_tag가 빈 요청 줄만 꼬리표 없음으로 채운다. 두 번 돌려도 같고, 이전한 DB를 2a 판의 쓰기가 그대로 쓸 수 있다.
"""

import sqlite3

from haniel_orch.event_store import EventStore

STAMP = "2026-08-01T00:00:00Z"

# 이 판이 더한 칸. 2a 판 DB를 흉내 낼 때 지운다
NEW_ATTEMPT_COLUMNS = ("attempt_seq", "execution_started_at", "late_evidence")
NEW_EVENT_COLUMNS = ("sm_tag", "applied_after_close_at", "applied_after_close_attempt_id")

# 2a 판(main 9b95a18)의 시도 줄 INSERT 칸 목록과 요청 줄 INSERT
PR2A_ATTEMPT_INSERT = (
    "INSERT INTO deploy_attempts (orchestrator_attempt_id, deploy_id, node_id, repo, branch, target_head, "
    "source, execution_mode, approved_by, probe_id, connection_generation, proposal_fingerprint, "
    "source_orchestrator_attempt_id, journal_attempt_id, manifest_identity, manifest_digest, journal_target_head, "
    "journal_completed_at, original_previous_head, commits_json, affected_services_json, diff_stat, detected_at, "
    "started_at, deadline_at, sm_evidence) VALUES (?, ?, 'n', ?, 'main', 'target', 'manual_single', 'execute', 'd', "
    "NULL, 'g1', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, '[]', '[]', NULL, ?, ?, '2099-01-01T00:00:00+00:00', "
    "'ev_none')"
)
PR2A_REQUEST_INSERT = (
    "INSERT OR IGNORE INTO deploy_events (deploy_id, node_id, repo, branch, status, sm_status, sm_lock, commits_json, "
    "affected_services_json, diff_stat, detected_at, created_at, updated_at, target_head, deployment_kind, "
    "expected_manifest_identity, expected_manifest_digest, is_self_update) "
    "VALUES (?, 'n', ?, 'main', 'pending', 'pending', 'l_off', '[]', '[]', NULL, ?, ?, ?, 'target', 'legacy', NULL, NULL, 0)"
)

# 요청 -> 시도 줄 (시도 번호, started_at, outcome, sm_evidence, terminal_kind). 삽입 순서와 started_at 순서를 어긋나게 둔다
ATTEMPTS = {
    "n:r1:main:many": [
        ("m-second", "2026-08-01T00:02:00Z", "failed", "ev_failed", "attempt_timeout"),
        ("m-first", "2026-08-01T00:01:00Z", "failed", "ev_failed", "deploy_result_failed"),
        ("m-third", "2026-08-01T00:03:00Z", "active", "ev_none", None),
    ],
    "n:r2:main:lease": [("l-only", "2026-08-01T00:01:00Z", "failed", "ev_failed", "attempt_timeout")],
    "n:r3:main:closed": [("c-only", "2026-08-01T00:01:00Z", "success", "ev_both", None)],
}
EVENT_STATUS = {  # deploy_id -> (status, sm_status, sm_lock)
    "n:r1:main:many": ("deploying", "deploying", "l_off"),
    "n:r2:main:lease": ("pending", "retry", "l_on"),  # 임대 마감으로 닫힌 시도가 켠 점검 필요
    "n:r3:main:closed": ("success", "success", "l_off"),
    "n:r4:main:rejected": ("rejected", "rejected", "l_off"),
}


async def make_pr2a_db(path) -> None:
    """2a 판 스키마의 DB를 만든다: 새 여섯 칸이 없고 옛 모양의 줄이 들어 있다."""
    seed = EventStore(str(path))
    await seed.initialize()
    await seed.close()
    con = sqlite3.connect(path)
    for column in NEW_ATTEMPT_COLUMNS:
        con.execute(f"ALTER TABLE deploy_attempts DROP COLUMN {column}")
    for column in NEW_EVENT_COLUMNS:
        con.execute(f"ALTER TABLE deploy_events DROP COLUMN {column}")
    for index, (deploy_id, (status, leaf, lock)) in enumerate(EVENT_STATUS.items(), 1):
        con.execute(PR2A_REQUEST_INSERT, (deploy_id, f"r{index}", STAMP, STAMP, STAMP))
        con.execute(
            "UPDATE deploy_events SET status = ?, sm_status = ?, sm_lock = ? WHERE deploy_id = ?",
            (status, leaf, lock, deploy_id),
        )
    for deploy_id, attempts in ATTEMPTS.items():
        for attempt_id, started_at, outcome, leaf, kind in attempts:
            con.execute(PR2A_ATTEMPT_INSERT, (attempt_id, deploy_id, deploy_id.split(":")[1], STAMP, started_at))
            con.execute(
                "UPDATE deploy_attempts SET outcome = ?, sm_evidence = ?, terminal_kind = ? WHERE orchestrator_attempt_id = ?",
                (outcome, leaf, kind, attempt_id),
            )
    con.execute(
        "INSERT INTO deploy_retry_requirements (deploy_id, source_orchestrator_attempt_id, "
        "last_failed_orchestrator_attempt_id, created_at, updated_at, inspection_required) VALUES (?, 'l-only', 'l-only', ?, ?, 1)",
        ("n:r2:main:lease", STAMP, STAMP),
    )
    con.execute("INSERT INTO deploy_retry_source_attempts VALUES ('n:r2:main:lease', 'l-only', ?)", (STAMP,))
    con.commit()
    con.close()


def columns(path, table) -> set[str]:
    con = sqlite3.connect(path)
    try:
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def one(path, sql, *params):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def dump(path) -> dict:
    return {
        table: one(path, f"SELECT * FROM {table} ORDER BY 1, 2")
        for table in ("deploy_events", "deploy_attempts", "deploy_retry_requirements", "deploy_retry_source_attempts")
    }


async def migrate(path) -> None:
    store = EventStore(str(path))
    await store.initialize()
    await store.close()


async def test_migration_adds_nullable_columns_and_keeps_rows_and_values(tmp_path):
    path = tmp_path / "pr2a.sqlite3"
    await make_pr2a_db(path)
    assert not (columns(path, "deploy_attempts") & set(NEW_ATTEMPT_COLUMNS))
    assert not (columns(path, "deploy_events") & set(NEW_EVENT_COLUMNS))
    attempts_before = one(path, "SELECT orchestrator_attempt_id, deploy_id, outcome, sm_evidence, terminal_kind, started_at FROM deploy_attempts ORDER BY 1")
    events_before = one(path, "SELECT deploy_id, status, sm_status, sm_lock, updated_at FROM deploy_events ORDER BY 1")
    markers_before = one(path, "SELECT * FROM deploy_retry_requirements")

    await migrate(path)

    assert set(NEW_ATTEMPT_COLUMNS) <= columns(path, "deploy_attempts")
    assert set(NEW_EVENT_COLUMNS) <= columns(path, "deploy_events")
    notnull = {row[1]: row[3] for row in one(path, "PRAGMA table_info(deploy_attempts)")}
    assert all(notnull[column] == 0 for column in NEW_ATTEMPT_COLUMNS)
    notnull = {row[1]: row[3] for row in one(path, "PRAGMA table_info(deploy_events)")}
    assert all(notnull[column] == 0 for column in NEW_EVENT_COLUMNS)
    # 줄 수와 값 보존. 기존 줄의 잎은 바꾸지 않는다(임대 마감으로 실패 잎이 된 옛 줄도 그대로)
    assert one(path, "SELECT orchestrator_attempt_id, deploy_id, outcome, sm_evidence, terminal_kind, started_at FROM deploy_attempts ORDER BY 1") == attempts_before
    assert one(path, "SELECT deploy_id, status, sm_status, sm_lock, updated_at FROM deploy_events ORDER BY 1") == events_before
    assert one(path, "SELECT * FROM deploy_retry_requirements") == markers_before
    # 새 칸의 기본: 실행 시작과 늦은 증거는 비고, 꼬리표는 없음, 적용 시각과 시도 번호는 빔
    assert one(path, "SELECT DISTINCT execution_started_at, late_evidence FROM deploy_attempts") == [(None, None)]
    assert one(path, "SELECT DISTINCT sm_tag, applied_after_close_at, applied_after_close_attempt_id FROM deploy_events") == [("t_off", None, None)]


async def test_attempt_seq_is_filled_per_request_in_started_at_order(tmp_path):
    path = tmp_path / "pr2a.sqlite3"
    await make_pr2a_db(path)

    await migrate(path)

    assert dict(one(path, "SELECT orchestrator_attempt_id, attempt_seq FROM deploy_attempts")) == {
        "m-first": 1,
        "m-second": 2,
        "m-third": 3,
        "l-only": 1,
        "c-only": 1,
    }


async def test_migration_twice_gives_the_same_database(tmp_path):
    path = tmp_path / "pr2a.sqlite3"
    await make_pr2a_db(path)
    await migrate(path)
    once = dump(path)

    await migrate(path)

    assert dump(path) == once


async def test_only_empty_seq_cells_are_filled_and_after_the_existing_maximum(tmp_path):
    """되돌렸다 다시 올린 DB: 2a 코드가 순서 칸 없이 넣은 줄은 그 요청의 최댓값 다음 번호를 받는다."""
    path = tmp_path / "pr2a.sqlite3"
    await make_pr2a_db(path)
    await migrate(path)
    con = sqlite3.connect(path)
    con.execute("UPDATE deploy_attempts SET outcome = 'failed', sm_evidence = 'ev_failed' WHERE orchestrator_attempt_id = 'm-third'")
    con.execute("UPDATE deploy_attempts SET attempt_seq = 7 WHERE orchestrator_attempt_id = 'm-third'")  # 손으로 고친 값도 그대로 둔다
    con.execute(PR2A_ATTEMPT_INSERT, ("m-rollback", "n:r1:main:many", "r1", STAMP, "2026-08-01T00:00:30Z"))  # 2a 판 쓰기
    con.commit()
    con.close()
    assert one(path, "SELECT attempt_seq FROM deploy_attempts WHERE orchestrator_attempt_id = 'm-rollback'") == [(None,)]

    await migrate(path)

    assert dict(one(path, "SELECT orchestrator_attempt_id, attempt_seq FROM deploy_attempts WHERE deploy_id = 'n:r1:main:many'")) == {
        "m-first": 1,
        "m-second": 2,
        "m-third": 7,
        "m-rollback": 8,  # started_at이 앞서도 이미 있는 번호 뒤에 붙는다(순서 칸은 「연 순서」지 시각이 아니다)
    }


async def test_migrated_database_accepts_the_pr2a_writes(tmp_path):
    path = tmp_path / "pr2a.sqlite3"
    await make_pr2a_db(path)
    await migrate(path)
    con = sqlite3.connect(path)

    con.execute(PR2A_REQUEST_INSERT, ("n:r9:main:new", "r9", STAMP, STAMP, STAMP))
    con.execute(PR2A_ATTEMPT_INSERT, ("new-attempt", "n:r9:main:new", "r9", STAMP, STAMP))
    con.commit()

    rows = con.execute("SELECT sm_tag, applied_after_close_at FROM deploy_events WHERE deploy_id = 'n:r9:main:new'").fetchall()
    attempt = con.execute("SELECT attempt_seq, execution_started_at, late_evidence FROM deploy_attempts WHERE orchestrator_attempt_id = 'new-attempt'").fetchall()
    con.close()
    assert rows == [(None, None)] and attempt == [(None, None, None)]  # 새 칸은 비고 삽입은 깨지지 않는다

    await migrate(path)  # 다음 기동이 빈 칸을 채운다
    assert one(path, "SELECT sm_tag FROM deploy_events WHERE deploy_id = 'n:r9:main:new'") == [("t_off",)]
    assert one(path, "SELECT attempt_seq FROM deploy_attempts WHERE orchestrator_attempt_id = 'new-attempt'") == [(1,)]
