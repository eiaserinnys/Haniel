"""Durable schema for deploy preflight, attempt ownership, and retry lineage."""

from __future__ import annotations

import aiosqlite

from .statechart.backfill import backfill_leaves


ATTEMPT_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS deploy_plan_probes (
    probe_id TEXT PRIMARY KEY,
    connection_generation TEXT NOT NULL,
    deploy_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    repo TEXT NOT NULL,
    branch TEXT NOT NULL,
    target_head TEXT NOT NULL,
    requested_orchestrator_attempt_id TEXT,
    source_orchestrator_attempt_id TEXT,
    retry_lineage_json TEXT NOT NULL DEFAULT '[]',
    expected_manifest_identity TEXT,
    expected_manifest_digest TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    mode TEXT,
    proposal_fingerprint TEXT,
    proposal_json TEXT,
    terminal_kind TEXT,
    terminal_stage TEXT,
    terminal_reason TEXT,
    terminal_error TEXT,
    created_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS deploy_attempts (
    orchestrator_attempt_id TEXT PRIMARY KEY,
    deploy_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    repo TEXT NOT NULL,
    branch TEXT NOT NULL,
    target_head TEXT NOT NULL,
    source TEXT NOT NULL,
    execution_mode TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    probe_id TEXT,
    connection_generation TEXT NOT NULL,
    proposal_fingerprint TEXT,
    source_orchestrator_attempt_id TEXT,
    journal_attempt_id TEXT,
    manifest_identity TEXT,
    manifest_digest TEXT,
    journal_target_head TEXT,
    journal_completed_at TEXT,
    original_previous_head TEXT,
    result_status TEXT,
    result_error TEXT,
    duration_ms INTEGER,
    settled_local_head TEXT,
    settled_remote_head TEXT,
    settled_at TEXT,
    outcome TEXT NOT NULL DEFAULT 'active',
    terminal_kind TEXT,
    terminal_stage TEXT,
    terminal_reason TEXT,
    terminal_error TEXT,
    commits_json TEXT NOT NULL,
    affected_services_json TEXT NOT NULL,
    diff_stat TEXT,
    detected_at TEXT NOT NULL,
    started_at TEXT NOT NULL,
    expected_budget_sec INTEGER,
    deadline_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_attempt_per_deploy
ON deploy_attempts(deploy_id) WHERE outcome = 'active';

CREATE UNIQUE INDEX IF NOT EXISTS one_active_attempt_per_branch
ON deploy_attempts(node_id, repo, branch) WHERE outcome = 'active';

CREATE TABLE IF NOT EXISTS deploy_retry_requirements (
    deploy_id TEXT PRIMARY KEY,
    source_orchestrator_attempt_id TEXT,
    last_failed_orchestrator_attempt_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    inspection_required INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS deploy_retry_source_attempts (
    deploy_id TEXT NOT NULL,
    lineage_orchestrator_attempt_id TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (deploy_id, lineage_orchestrator_attempt_id)
);

CREATE TABLE IF NOT EXISTS node_deploy_attempts (
    node_attempt_id TEXT PRIMARY KEY,
    journal_attempt_id TEXT,
    deploy_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    repo TEXT NOT NULL,
    branch TEXT NOT NULL,
    target_head TEXT NOT NULL,
    trigger TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'active',
    started_local_head TEXT NOT NULL,
    terminal_local_head TEXT,
    error TEXT,
    duration_ms INTEGER,
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS node_deploy_attempts_by_deploy
ON node_deploy_attempts(deploy_id, completed_at);
"""

CANONICAL_COLUMNS = {
    "target_head": "TEXT",
    "deployment_kind": "TEXT NOT NULL DEFAULT 'legacy'",
    "expected_manifest_identity": "TEXT",
    "expected_manifest_digest": "TEXT",
    "is_self_update": "INTEGER NOT NULL DEFAULT 0",
    "postponed_until": "TEXT",
    # 요청 상태의 잎(statechart.definitions.OrchRequest). 정본이고 status는 바깥 칸이다
    "sm_status": "TEXT",
    # 점검 필요의 잎(같은 정의의 lock 영역). 바깥 칸은 표시 줄의 inspection_required다
    "sm_lock": "TEXT",
}

ATTEMPT_COLUMNS = {
    "expected_budget_sec": "INTEGER",
    "terminal_stage": "TEXT",
    "terminal_reason": "TEXT",
    # 시도 종결 증거의 잎(statechart.definitions.AttemptEvidence). outcome은 바깥 칸이다
    "sm_evidence": "TEXT",
}


async def _ensure_columns(
    db: aiosqlite.Connection, table: str, columns: dict[str, str]
) -> set[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in await cursor.fetchall()}
    added: set[str] = set()
    for name, declaration in columns.items():
        if name not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            added.add(name)
    return added


# 표시 줄의 두 시도 번호 칸 NOT NULL을 풀고 inspection_required를 더하려면 표를 다시 만들어야 한다(SQLite는 ALTER로
# NOT NULL을 풀지 못한다). 옛 줄은 모두 inspection_required=1(옛 뜻: 표시 줄이 있으면 점검 필요)이 된다.
_NEW_RETRY_TABLE_SQL = """CREATE TABLE deploy_retry_requirements_new (
    deploy_id TEXT PRIMARY KEY,
    source_orchestrator_attempt_id TEXT,
    last_failed_orchestrator_attempt_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    inspection_required INTEGER NOT NULL DEFAULT 1
)"""


async def _retry_table_is_migrated(db: aiosqlite.Connection) -> bool:
    cursor = await db.execute("PRAGMA table_info(deploy_retry_requirements)")
    not_null = {row[1]: row[3] for row in await cursor.fetchall()}
    return (
        "inspection_required" in not_null
        and not not_null["source_orchestrator_attempt_id"]
        and not not_null["last_failed_orchestrator_attempt_id"]
    )


async def _migrate_retry_requirements(db: aiosqlite.Connection) -> None:
    """표시 줄 표를 한 트랜잭션으로 다시 만든다. 이미 바뀌었으면 아무것도 하지 않는다(두 번 돌아도 같다)."""
    if await _retry_table_is_migrated(db):
        return
    if db.in_transaction:
        await db.commit()
    await db.execute("BEGIN IMMEDIATE")
    try:
        await db.execute("DROP TABLE IF EXISTS deploy_retry_requirements_new")
        await db.execute(_NEW_RETRY_TABLE_SQL)
        await db.execute(
            """INSERT INTO deploy_retry_requirements_new
               (deploy_id, source_orchestrator_attempt_id,
                last_failed_orchestrator_attempt_id, created_at, updated_at,
                inspection_required)
               SELECT deploy_id, source_orchestrator_attempt_id,
                      last_failed_orchestrator_attempt_id, created_at, updated_at, 1
               FROM deploy_retry_requirements"""
        )
        await db.execute("DROP TABLE deploy_retry_requirements")
        await db.execute(
            "ALTER TABLE deploy_retry_requirements_new "
            "RENAME TO deploy_retry_requirements"
        )
        await db.execute("COMMIT")
    except BaseException:
        await db.execute("ROLLBACK")
        raise


async def initialize_attempt_schema(db: aiosqlite.Connection) -> None:
    """Add v16 tables without reopening historical FAILED rows or journals."""
    canonical_added = await _ensure_columns(db, "deploy_events", CANONICAL_COLUMNS)
    if "is_self_update" in canonical_added:
        await db.execute(
            """UPDATE deploy_events SET is_self_update = 1
               WHERE lower(repo) = 'haniel' AND status IN ('pending', 'deploying')"""
        )
    await db.executescript(ATTEMPT_TABLES_SQL)
    await _migrate_retry_requirements(db)
    await _ensure_columns(db, "deploy_attempts", ATTEMPT_COLUMNS)
    # 잎이 비었거나 바깥 칸과 어긋난 줄만 옛 칸에서 다시 계산한다
    await backfill_leaves(db)
