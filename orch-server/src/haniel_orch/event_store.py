"""SQLite-backed persistence for deploy events and node registry."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiosqlite

from . import event_store_mutations, event_store_nodes
from .deploy_attempt_schema import initialize_attempt_schema
from .deploy_attempt_store import DeployAttemptStore
from .event_store_lifecycle import EventStoreLifecycleMixin
from .event_store_rows import decode_deploy_row, now_iso
from .protocol import DeployStatus

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS deploy_events (
    deploy_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    repo TEXT NOT NULL,
    branch TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    commits_json TEXT NOT NULL,
    affected_services_json TEXT NOT NULL,
    diff_stat TEXT,
    detected_at TEXT NOT NULL,
    approved_by TEXT,
    reject_reason TEXT,
    error TEXT,
    duration_ms INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    is_self_update INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    hostname TEXT NOT NULL,
    os TEXT NOT NULL,
    arch TEXT NOT NULL,
    haniel_version TEXT NOT NULL,
    connected INTEGER NOT NULL DEFAULT 1,
    connection_generation TEXT,
    connection_token TEXT,
    last_seen TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS device_tokens (
    token_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (node_id) REFERENCES nodes(node_id)
);
"""


class EventStore(EventStoreLifecycleMixin):
    """Async SQLite store for deploy events and nodes."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._mutation_lock = asyncio.Lock()
        self.attempts: DeployAttemptStore | None = None

    async def initialize(self) -> None:
        """Create tables if they don't exist."""
        async with self._mutation_lock:
            self._db = await aiosqlite.connect(self._db_path)
            await self._db.executescript(_CREATE_TABLES)
            await event_store_nodes.initialize_node_schema(self._db)
            await initialize_attempt_schema(self._db)
            self.attempts = DeployAttemptStore(self._db, self._mutation_lock)
            await self._db.commit()

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    async def create_deploy_event(
        self,
        deploy_id: str,
        node_id: str,
        repo: str,
        branch: str,
        commits: list[str],
        affected_services: list[str],
        diff_stat: str | None,
        detected_at: str,
        target_head: str | None = None,
        deployment_kind: str = "legacy",
        expected_manifest_identity: str | None = None,
        expected_manifest_digest: str | None = None,
        is_self_update: bool = False,
    ) -> bool:
        """Create a deploy event.

        Returns True when a new row was inserted. Duplicate deploy_id is
        silently ignored and returns False.
        """
        async with self._mutation_lock:
            now = now_iso()
            try:
                cursor = await self._db.execute(
                    """INSERT OR IGNORE INTO deploy_events
                       (deploy_id, node_id, repo, branch, status,
                        commits_json, affected_services_json, diff_stat, detected_at,
                        created_at, updated_at, target_head, deployment_kind,
                        expected_manifest_identity, expected_manifest_digest,
                        is_self_update)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        deploy_id,
                        node_id,
                        repo,
                        branch,
                        DeployStatus.PENDING.value,
                        json.dumps(commits),
                        json.dumps(affected_services),
                        diff_stat,
                        detected_at,
                        now,
                        now,
                        target_head or deploy_id.rsplit(":", 1)[-1],
                        deployment_kind,
                        expected_manifest_identity,
                        expected_manifest_digest,
                        int(is_self_update),
                    ),
                )
                if cursor.rowcount > 0:
                    latest_cursor = await self._db.execute(
                        "SELECT deploy_id FROM deploy_events WHERE node_id = ? "
                        "AND repo = ? AND branch = ? AND deploy_id NOT LIKE 'attempt:%' "
                        "ORDER BY detected_at DESC, created_at DESC, deploy_id DESC LIMIT 1",
                        (node_id, repo, branch),
                    )
                    latest_row = await latest_cursor.fetchone()
                    if latest_row is None:
                        raise RuntimeError("inserted canonical disappeared")
                    latest_id = latest_row[0]
                    pending_cursor = await self._db.execute(
                        "SELECT deploy_id FROM deploy_events WHERE node_id = ? AND repo = ? "
                        "AND branch = ? AND status = ? AND deploy_id != ?",
                        (
                            node_id,
                            repo,
                            branch,
                            DeployStatus.PENDING.value,
                            latest_id,
                        ),
                    )
                    for (old_id,) in await pending_cursor.fetchall():
                        await self._db.execute(
                            "UPDATE deploy_events SET status = ?, reject_reason = ?, updated_at = ? "
                            "WHERE deploy_id = ? AND status = ?",
                            (
                                DeployStatus.REJECTED.value,
                                f"superseded by {latest_id}",
                                now,
                                old_id,
                                DeployStatus.PENDING.value,
                            ),
                        )
                        await self._db.execute(
                            "DELETE FROM deploy_retry_requirements WHERE deploy_id = ?",
                            (old_id,),
                        )
                        await self._db.execute(
                            "DELETE FROM deploy_retry_source_attempts WHERE deploy_id = ?",
                            (old_id,),
                        )
                        await self._db.execute(
                            """UPDATE deploy_plan_probes
                               SET status = 'terminal', terminal_kind = 'preflight_stale',
                                   terminal_stage = 'canonical_create',
                                   terminal_reason = 'newer_canonical',
                                   terminal_error = ?, completed_at = ?
                               WHERE deploy_id = ? AND status IN ('active','proposed')""",
                            (
                                f"canonical superseded by {latest_id}",
                                now,
                                old_id,
                            ),
                        )
                await self._db.commit()
                return cursor.rowcount > 0
            except Exception:
                await self._db.rollback()
                raise

    async def transition_deploy_status(
        self,
        deploy_id: str,
        expected: DeployStatus,
        target: DeployStatus,
    ) -> bool:
        """Perform one guarded lifecycle transition."""
        async with self._mutation_lock:
            try:
                transitioned = await event_store_mutations.transition_deploy_status(
                    self._db, deploy_id, expected, target
                )
                await self._db.commit()
                return transitioned
            except Exception:
                await self._db.rollback()
                raise

    async def resolve_observed_pending(
        self,
        *,
        deploy_id: str,
        node_id: str,
        repo: str,
        branch: str,
        local_head: str,
        remote_head: str,
    ) -> bool:
        """Resolve one exact non-retry stale row from observed Git truth."""
        async with self._mutation_lock:
            try:
                cursor = await self._db.execute(
                    """UPDATE deploy_events SET status = ?, updated_at = ?
                       WHERE deploy_id = ? AND node_id = ? AND repo = ? AND branch = ?
                         AND status = ? AND target_head = ? AND ? = ?
                         AND NOT EXISTS (
                           SELECT 1 FROM deploy_retry_requirements r
                           WHERE r.deploy_id = deploy_events.deploy_id
                         )
                         AND NOT EXISTS (
                           SELECT 1 FROM deploy_attempts a
                           WHERE a.deploy_id = deploy_events.deploy_id
                             AND a.outcome = 'active'
                         )""",
                    (
                        DeployStatus.SUCCESS.value,
                        now_iso(),
                        deploy_id,
                        node_id,
                        repo,
                        branch,
                        DeployStatus.PENDING.value,
                        local_head,
                        local_head,
                        remote_head,
                    ),
                )
                await self._db.commit()
                return cursor.rowcount == 1
            except Exception:
                await self._db.rollback()
                raise

    async def get_deploy_event(self, deploy_id: str) -> dict[str, Any] | None:
        """Get a single deploy event by ID. Returns None if not found."""
        cursor = await self._db.execute(
            "SELECT * FROM deploy_events WHERE deploy_id = ?", (deploy_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return decode_deploy_row(cursor, row)

    async def get_pending_deploys(self) -> list[dict[str, Any]]:
        """Get all events with status='pending'.

        Used by approve_all (PENDING-only semantics — DEPLOYING shouldn't be re-approved).
        For dashboard's PendingView (which shows pending+deploying), use
        ``get_active_deploys`` instead.
        """
        cursor = await self._db.execute(
            "SELECT * FROM deploy_events WHERE status = ? "
            "ORDER BY detected_at DESC, created_at DESC, deploy_id DESC",
            (DeployStatus.PENDING.value,),
        )
        rows = await cursor.fetchall()
        return [decode_deploy_row(cursor, row) for row in rows]

    async def get_active_deploys(self) -> list[dict[str, Any]]:
        """Get pending + deploying events (newest first).

        Used by /api/orch/pending so that PendingView keeps showing deploys
        that have advanced to DEPLOYING after approval. APPROVED is transient
        and not included — the hub flips APPROVED → DEPLOYING immediately
        on send_to_node.
        """
        cursor = await self._db.execute(
            "SELECT * FROM deploy_events WHERE status IN (?, ?) "
            "ORDER BY detected_at DESC, created_at DESC, deploy_id DESC",
            (DeployStatus.PENDING.value, DeployStatus.DEPLOYING.value),
        )
        rows = await cursor.fetchall()
        return [decode_deploy_row(cursor, row) for row in rows]

    async def get_active_self_update_for_node(
        self, node_id: str
    ) -> dict[str, Any] | None:
        """Return one active self-update that blocks other deploys on a node."""
        cursor = await self._db.execute(
            "SELECT * FROM deploy_events "
            "WHERE node_id = ? AND is_self_update = 1 AND status IN (?, ?) "
            "ORDER BY detected_at DESC, created_at DESC, deploy_id DESC LIMIT 1",
            (
                node_id,
                DeployStatus.PENDING.value,
                DeployStatus.DEPLOYING.value,
            ),
        )
        row = await cursor.fetchone()
        return None if row is None else decode_deploy_row(cursor, row)

    async def supersede_stale_pending_deploys(self) -> list[str]:
        """Reject older PENDING deploys per (node, repo, branch).

        This startup repair handles rows that existed before generation-time
        supersede was added, or rows inserted while an old orch-server was
        running. Read APIs never invoke it. DEPLOYING rows are excluded.
        """
        async with self._mutation_lock:
            try:
                superseded = (
                    await event_store_mutations.supersede_stale_pending_deploys(
                        self._db
                    )
                )
                await self._db.commit()
                return superseded
            except Exception:
                await self._db.rollback()
                raise

    async def get_pending_deploys_for_branch(
        self, node_id: str, repo: str, branch: str
    ) -> list[dict[str, Any]]:
        """Get PENDING deploys for the same (node, repo, branch).

        Used by hub.supersede_pending to mark older PENDING deploys as
        REJECTED (with reject_reason='superseded by ${kept}') when a newer
        deploy is approved.
        """
        cursor = await self._db.execute(
            "SELECT * FROM deploy_events "
            "WHERE node_id = ? AND repo = ? AND branch = ? AND status = ? "
            "ORDER BY created_at DESC",
            (node_id, repo, branch, DeployStatus.PENDING.value),
        )
        rows = await cursor.fetchall()
        return [decode_deploy_row(cursor, row) for row in rows]

    async def supersede_pending_for_branch(
        self, node_id: str, repo: str, branch: str
    ) -> list[dict[str, Any]]:
        async with self._mutation_lock:
            try:
                rejected = await event_store_mutations.supersede_pending_for_branch(
                    self._db, node_id, repo, branch
                )
                await self._db.commit()
                return rejected
            except Exception:
                await self._db.rollback()
                raise

    async def get_deploys_superseded_by(self, deploy_id: str) -> list[dict[str, Any]]:
        """Read canonical rows atomically superseded when ``deploy_id`` was created."""
        cursor = await self._db.execute(
            "SELECT * FROM deploy_events WHERE status = ? AND reject_reason = ?",
            (DeployStatus.REJECTED.value, f"superseded by {deploy_id}"),
        )
        rows = await cursor.fetchall()
        return [decode_deploy_row(cursor, row) for row in rows]
