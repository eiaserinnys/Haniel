"""SQLite-backed persistence for deploy events and node registry."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from . import event_store_mutations, event_store_nodes
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
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    hostname TEXT NOT NULL,
    os TEXT NOT NULL,
    arch TEXT NOT NULL,
    haniel_version TEXT NOT NULL,
    connected INTEGER NOT NULL DEFAULT 1,
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(cursor: aiosqlite.Cursor, row: tuple) -> dict[str, Any]:
    """Convert a row tuple to a dict using cursor.description."""
    return {col[0]: val for col, val in zip(cursor.description, row)}


class EventStore:
    """Async SQLite store for deploy events and nodes."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._mutation_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Create tables if they don't exist."""
        async with self._mutation_lock:
            self._db = await aiosqlite.connect(self._db_path)
            await self._db.executescript(_CREATE_TABLES)
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
    ) -> bool:
        """Create a deploy event.

        Returns True when a new row was inserted. Duplicate deploy_id is
        silently ignored and returns False.
        """
        async with self._mutation_lock:
            now = _now_iso()
            try:
                cursor = await self._db.execute(
                    """INSERT OR IGNORE INTO deploy_events
                       (deploy_id, node_id, repo, branch, status,
                        commits_json, affected_services_json, diff_stat, detected_at,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    ),
                )
                await self._db.commit()
                return cursor.rowcount > 0
            except Exception:
                await self._db.rollback()
                raise

    async def reopen_failed_deploy(self, deploy_id: str) -> bool:
        """Snapshot one failed attempt and conditionally reopen its canonical ID."""
        async with self._mutation_lock:
            try:
                reopened = await event_store_mutations.reopen_failed_deploy(
                    self._db, deploy_id
                )
                await self._db.commit()
                return reopened
            except Exception:
                await self._db.rollback()
                raise

    async def apply_deploy_result(
        self,
        deploy_id: str,
        status: DeployStatus,
        *,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> bool:
        """Apply a result only to the currently deploying canonical attempt."""
        if status not in (DeployStatus.SUCCESS, DeployStatus.FAILED):
            raise ValueError("deploy result must be success or failed")
        async with self._mutation_lock:
            try:
                applied = await event_store_mutations.apply_deploy_result(
                    self._db, deploy_id, status, error, duration_ms
                )
                await self._db.commit()
                return applied
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

    async def resolve_pending_branch(
        self, node_id: str, repo: str, branch: str
    ) -> list[str]:
        """Mark branch PENDING rows successful when local and remote HEAD agree."""
        async with self._mutation_lock:
            try:
                resolved = await event_store_mutations.resolve_pending_branch(
                    self._db, node_id, repo, branch
                )
                await self._db.commit()
                return resolved
            except Exception:
                await self._db.rollback()
                raise

    async def get_latest_failed_deploy(self) -> dict[str, Any] | None:
        cursor = await self._db.execute(
            "SELECT * FROM deploy_events WHERE status = ? "
            "ORDER BY updated_at DESC, deploy_id DESC LIMIT 1",
            (DeployStatus.FAILED.value,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        result = _row_to_dict(cursor, row)
        result["commits"] = json.loads(result.pop("commits_json"))
        result["affected_services"] = json.loads(result.pop("affected_services_json"))
        return result

    async def get_deploy_event(self, deploy_id: str) -> dict[str, Any] | None:
        """Get a single deploy event by ID. Returns None if not found."""
        cursor = await self._db.execute(
            "SELECT * FROM deploy_events WHERE deploy_id = ?", (deploy_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        result = _row_to_dict(cursor, row)
        result["commits"] = json.loads(result.pop("commits_json"))
        result["affected_services"] = json.loads(result.pop("affected_services_json"))
        return result

    async def get_pending_deploys(self) -> list[dict[str, Any]]:
        """Get all events with status='pending'.

        Used by approve_all (PENDING-only semantics — DEPLOYING shouldn't be re-approved).
        For dashboard's PendingView (which shows pending+deploying), use
        ``get_active_deploys`` instead.
        """
        cursor = await self._db.execute(
            "SELECT * FROM deploy_events WHERE status = ? ORDER BY created_at DESC",
            (DeployStatus.PENDING.value,),
        )
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            d = _row_to_dict(cursor, row)
            d["commits"] = json.loads(d.pop("commits_json"))
            d["affected_services"] = json.loads(d.pop("affected_services_json"))
            results.append(d)
        return results

    async def get_active_deploys(self) -> list[dict[str, Any]]:
        """Get pending + deploying events (newest first).

        Used by /api/orch/pending so that PendingView keeps showing deploys
        that have advanced to DEPLOYING after approval. APPROVED is transient
        and not included — the hub flips APPROVED → DEPLOYING immediately
        on send_to_node.
        """
        await self.supersede_stale_pending_deploys()
        cursor = await self._db.execute(
            "SELECT * FROM deploy_events WHERE status IN (?, ?) "
            "ORDER BY created_at DESC",
            (DeployStatus.PENDING.value, DeployStatus.DEPLOYING.value),
        )
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            d = _row_to_dict(cursor, row)
            d["commits"] = json.loads(d.pop("commits_json"))
            d["affected_services"] = json.loads(d.pop("affected_services_json"))
            results.append(d)
        return results

    async def supersede_stale_pending_deploys(self) -> list[str]:
        """Reject older PENDING deploys per (node, repo, branch).

        This is the read/repair gate for rows that existed before
        generation-time supersede was added, or for rows inserted while an old
        orch-server was running. DEPLOYING rows are intentionally excluded.
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
        results = []
        for row in rows:
            d = _row_to_dict(cursor, row)
            d["commits"] = json.loads(d.pop("commits_json"))
            d["affected_services"] = json.loads(d.pop("affected_services_json"))
            results.append(d)
        return results

    async def get_deploy_history(
        self,
        limit: int = 50,
        *,
        include_superseded: bool = False,
    ) -> list[dict[str, Any]]:
        """Get deploy events newest first.

        By default, auto-supersede entries (status=rejected with
        reject_reason starting with 'superseded by ') are excluded so the
        dashboard history shows actionable deploys only. Pass
        ``include_superseded=True`` to include them (audit view exposed via
        ``GET /api/orch/history?include_superseded=1``).

        Manual rejects (operator-provided reject_reason) are NOT filtered —
        only the auto-supersede marker prefix is recognised.
        """
        if include_superseded:
            sql = "SELECT * FROM deploy_events ORDER BY created_at DESC LIMIT ?"
            params: tuple[Any, ...] = (limit,)
        else:
            sql = (
                "SELECT * FROM deploy_events "
                "WHERE NOT (status = ? AND reject_reason LIKE 'superseded by %') "
                "ORDER BY created_at DESC LIMIT ?"
            )
            params = (DeployStatus.REJECTED.value, limit)
        cursor = await self._db.execute(sql, params)
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            d = _row_to_dict(cursor, row)
            d["commits"] = json.loads(d.pop("commits_json"))
            d["affected_services"] = json.loads(d.pop("affected_services_json"))
            results.append(d)
        return results

    async def update_deploy_status(
        self,
        deploy_id: str,
        status: DeployStatus,
        approved_by: str | None = None,
        reject_reason: str | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Update event status and optional fields. Only non-None args are SET."""
        async with self._mutation_lock:
            try:
                await self._update_deploy_status_unlocked(
                    deploy_id, status, approved_by, reject_reason, error, duration_ms
                )
                await self._db.commit()
            except Exception:
                await self._db.rollback()
                raise

    async def _update_deploy_status_unlocked(
        self,
        deploy_id: str,
        status: DeployStatus,
        approved_by: str | None = None,
        reject_reason: str | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        updates = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status.value, _now_iso()]

        if approved_by is not None:
            updates.append("approved_by = ?")
            params.append(approved_by)
        if reject_reason is not None:
            updates.append("reject_reason = ?")
            params.append(reject_reason)
        if error is not None:
            updates.append("error = ?")
            params.append(error)
        if duration_ms is not None:
            updates.append("duration_ms = ?")
            params.append(duration_ms)

        params.append(deploy_id)
        await self._db.execute(
            f"UPDATE deploy_events SET {', '.join(updates)} WHERE deploy_id = ?",
            params,
        )

    async def get_deploying_events_for_node(self, node_id: str) -> list[dict[str, Any]]:
        """Get events in DEPLOYING state for a specific node.

        Used by WebSocketHub._cleanup_orphan_deploys() to mark in-flight
        deploys as FAILED on node disconnect (ws-disconnect, heartbeat-timeout,
        and shutdown share this single source of truth).
        """
        cursor = await self._db.execute(
            "SELECT * FROM deploy_events WHERE node_id = ? AND status = ?",
            (node_id, DeployStatus.DEPLOYING.value),
        )
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            d = _row_to_dict(cursor, row)
            d["commits"] = json.loads(d.pop("commits_json"))
            d["affected_services"] = json.loads(d.pop("affected_services_json"))
            results.append(d)
        return results

    async def reject_pending_deploys_for_nodes(
        self, node_ids: list[str], reject_reason: str
    ) -> list[dict[str, Any]]:
        """Reject all PENDING deploys for the given nodes and return them."""
        if not node_ids:
            return []
        async with self._mutation_lock:
            try:
                rejected = await event_store_mutations.reject_pending_deploys_for_nodes(
                    self._db, node_ids, reject_reason
                )
                await self._db.commit()
                return rejected
            except Exception:
                await self._db.rollback()
                raise

    # --- nodes CRUD ---

    async def upsert_node(
        self,
        node_id: str,
        hostname: str,
        os: str,
        arch: str,
        haniel_version: str,
        connected: bool = True,
    ) -> None:
        """Register or update a node. INSERT OR REPLACE."""
        async with self._mutation_lock:
            try:
                await event_store_nodes.upsert_node(
                    self._db, node_id, hostname, os, arch, haniel_version, connected
                )
                await self._db.commit()
            except Exception:
                await self._db.rollback()
                raise

    async def update_node_heartbeat(self, node_id: str) -> None:
        """Update last_seen timestamp for a node."""
        async with self._mutation_lock:
            try:
                await event_store_nodes.update_node_heartbeat(self._db, node_id)
                await self._db.commit()
            except Exception:
                await self._db.rollback()
                raise

    async def mark_node_disconnected(self, node_id: str) -> None:
        """Mark an existing node disconnected without clobbering its metadata."""
        async with self._mutation_lock:
            try:
                await event_store_nodes.mark_node_disconnected(self._db, node_id)
                await self._db.commit()
            except Exception:
                await self._db.rollback()
                raise

    async def get_nodes(self) -> list[dict[str, Any]]:
        """Get all nodes (connected and disconnected)."""
        cursor = await self._db.execute("SELECT * FROM nodes ORDER BY last_seen DESC")
        rows = await cursor.fetchall()
        return [_row_to_dict(cursor, row) for row in rows]

    async def get_node_ids_by_hostname(self, hostname: str) -> list[str]:
        """Return node IDs that have reported the given hostname."""
        cursor = await self._db.execute(
            "SELECT node_id FROM nodes WHERE hostname = ?",
            (hostname,),
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]
