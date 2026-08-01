"""SQLite-backed persistence for deploy events and node registry."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from . import event_store_mutations, event_store_nodes
from .deploy_attempt_schema import initialize_attempt_schema
from .deploy_attempt_store import DeployAttemptStore
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
                        created_at, updated_at, target_head, deployment_kind,
                        expected_manifest_identity, expected_manifest_digest)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                        _now_iso(),
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
            "SELECT * FROM deploy_events WHERE status = ? "
            "ORDER BY detected_at DESC, created_at DESC, deploy_id DESC",
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
        cursor = await self._db.execute(
            "SELECT * FROM deploy_events WHERE status IN (?, ?) "
            "ORDER BY detected_at DESC, created_at DESC, deploy_id DESC",
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
        results = []
        for row in rows:
            d = _row_to_dict(cursor, row)
            d["commits"] = json.loads(d.pop("commits_json"))
            d["affected_services"] = json.loads(d.pop("affected_services_json"))
            results.append(d)
        return results

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
        results = []
        for row in rows:
            item = _row_to_dict(cursor, row)
            item["commits"] = json.loads(item.pop("commits_json"))
            item["affected_services"] = json.loads(item.pop("affected_services_json"))
            results.append(item)
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
            sql = "SELECT * FROM deploy_events"
            params: tuple[Any, ...] = ()
        else:
            sql = (
                "SELECT * FROM deploy_events "
                "WHERE NOT (status = ? AND reject_reason LIKE 'superseded by %') "
                ""
            )
            params = (DeployStatus.REJECTED.value,)
        cursor = await self._db.execute(sql, params)
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            d = _row_to_dict(cursor, row)
            d["commits"] = json.loads(d.pop("commits_json"))
            d["affected_services"] = json.loads(d.pop("affected_services_json"))
            d.update(
                terminal_kind=None,
                terminal_stage=None,
                terminal_reason=None,
                terminal_error=None,
            )
            results.append(d)
        if self.attempts is not None:
            success_metadata = await self.attempts.success_metadata()
            for item in results:
                metadata = success_metadata.get(item["deploy_id"])
                if (
                    metadata is not None
                    and item["status"] == DeployStatus.SUCCESS.value
                ):
                    item.update(metadata)
            results.extend(await self.attempts.history_rows(include_superseded))
        results.sort(
            key=lambda item: (
                item.get("updated_at") or item.get("created_at") or "",
                item["deploy_id"],
            ),
            reverse=True,
        )
        return results[:limit]

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
                if status in (DeployStatus.REJECTED, DeployStatus.SUCCESS):
                    await self._db.execute(
                        "DELETE FROM deploy_retry_requirements WHERE deploy_id = ?",
                        (deploy_id,),
                    )
                    await self._db.execute(
                        "DELETE FROM deploy_retry_source_attempts WHERE deploy_id = ?",
                        (deploy_id,),
                    )
                    await self._db.execute(
                        """UPDATE deploy_plan_probes
                           SET status = 'terminal', terminal_kind = 'preflight_stale',
                               terminal_stage = 'canonical_status_update',
                               terminal_reason = 'canonical_terminal',
                               terminal_error = ?, completed_at = ?
                           WHERE deploy_id = ? AND status IN ('active','proposed')""",
                        (
                            f"canonical became {status.value} during preflight",
                            _now_iso(),
                            deploy_id,
                        ),
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
        """Get events in DEPLOYING state for diagnostics."""
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
        connection_generation: str | None = None,
        connection_token: str | None = None,
    ) -> None:
        """Register or update a node and its durable connection generation."""
        async with self._mutation_lock:
            try:
                await event_store_nodes.upsert_node(
                    self._db,
                    node_id,
                    hostname,
                    os,
                    arch,
                    haniel_version,
                    connected,
                    connection_generation,
                    connection_token,
                )
                await self._db.commit()
            except Exception:
                await self._db.rollback()
                raise

    async def update_node_heartbeat(
        self,
        node_id: str,
        expected_generation: str | None = None,
        expected_connection_token: str | None = None,
    ) -> bool:
        """Update last_seen timestamp for a node."""
        async with self._mutation_lock:
            try:
                updated = await event_store_nodes.update_node_heartbeat(
                    self._db,
                    node_id,
                    expected_generation,
                    expected_connection_token,
                )
                await self._db.commit()
                return updated
            except Exception:
                await self._db.rollback()
                raise

    async def mark_node_disconnected(
        self,
        node_id: str,
        expected_generation: str,
        expected_connection_token: str,
    ) -> bool:
        """Mark only the expected live generation disconnected."""
        async with self._mutation_lock:
            try:
                updated = await event_store_nodes.mark_node_disconnected(
                    self._db,
                    node_id,
                    expected_generation,
                    expected_connection_token,
                )
                await self._db.commit()
                return updated
            except Exception:
                await self._db.rollback()
                raise

    async def get_node_connection_generation(self, node_id: str) -> str | None:
        """Return the durable connection generation for race assertions."""
        cursor = await self._db.execute(
            "SELECT connection_generation FROM nodes WHERE node_id = ?", (node_id,)
        )
        row = await cursor.fetchone()
        return None if row is None else row[0]

    async def get_node_connection_identity(
        self, node_id: str
    ) -> tuple[str | None, str | None] | None:
        """Return the durable generation and token used by disconnect CAS."""
        cursor = await self._db.execute(
            "SELECT connection_generation, connection_token FROM nodes WHERE node_id = ?",
            (node_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else (row[0], row[1])

    async def get_nodes(self) -> list[dict[str, Any]]:
        """Get all nodes (connected and disconnected)."""
        cursor = await self._db.execute(
            "SELECT node_id, hostname, os, arch, haniel_version, connected, "
            "last_seen, created_at FROM nodes ORDER BY last_seen DESC"
        )
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
