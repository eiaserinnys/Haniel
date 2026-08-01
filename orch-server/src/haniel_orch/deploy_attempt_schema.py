"""Durable schema for deploy preflight, attempt ownership, and retry lineage."""

from __future__ import annotations

import aiosqlite


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
    terminal_error TEXT,
    commits_json TEXT NOT NULL,
    affected_services_json TEXT NOT NULL,
    diff_stat TEXT,
    detected_at TEXT NOT NULL,
    started_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_attempt_per_deploy
ON deploy_attempts(deploy_id) WHERE outcome = 'active';

CREATE UNIQUE INDEX IF NOT EXISTS one_active_attempt_per_branch
ON deploy_attempts(node_id, repo, branch) WHERE outcome = 'active';

CREATE TABLE IF NOT EXISTS deploy_retry_requirements (
    deploy_id TEXT PRIMARY KEY,
    source_orchestrator_attempt_id TEXT NOT NULL,
    last_failed_orchestrator_attempt_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deploy_retry_source_attempts (
    deploy_id TEXT NOT NULL,
    lineage_orchestrator_attempt_id TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (deploy_id, lineage_orchestrator_attempt_id)
);
"""

CANONICAL_COLUMNS = {
    "target_head": "TEXT",
    "deployment_kind": "TEXT NOT NULL DEFAULT 'legacy'",
    "expected_manifest_identity": "TEXT",
    "expected_manifest_digest": "TEXT",
}


async def initialize_attempt_schema(db: aiosqlite.Connection) -> None:
    """Add v16 tables without reopening historical FAILED rows or journals."""
    cursor = await db.execute("PRAGMA table_info(deploy_events)")
    existing = {row[1] for row in await cursor.fetchall()}
    for name, declaration in CANONICAL_COLUMNS.items():
        if name not in existing:
            await db.execute(
                f"ALTER TABLE deploy_events ADD COLUMN {name} {declaration}"
            )
    await db.executescript(ATTEMPT_TABLES_SQL)

