"""SQLite-backed persistence for deploy events and node registry."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite

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

    async def initialize(self) -> None:
        """Create tables if they don't exist."""
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.executescript(_CREATE_TABLES)
        await self._db.commit()

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    # --- deploy_events CRUD ---

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
    ) -> None:
        """INSERT OR IGNORE — duplicate deploy_id is silently ignored."""
        now = _now_iso()
        await self._db.execute(
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
        result["affected_services"] = json.loads(
            result.pop("affected_services_json")
        )
        return result

    async def get_pending_deploys(self) -> list[dict[str, Any]]:
        """Get all events with status='pending'."""
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

    async def get_deploy_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get all deploy events, newest first."""
        cursor = await self._db.execute(
            "SELECT * FROM deploy_events ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
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
        await self._db.commit()

    async def get_deploying_events_for_node(
        self, node_id: str
    ) -> list[dict[str, Any]]:
        """Get events in DEPLOYING state for a specific node.

        Used by NodeRegistry.unregister() to mark in-flight deploys as FAILED.
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
        now = _now_iso()
        await self._db.execute(
            """INSERT OR REPLACE INTO nodes
               (node_id, hostname, os, arch, haniel_version, connected, last_seen, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (node_id, hostname, os, arch, haniel_version, int(connected), now, now),
        )
        await self._db.commit()

    async def update_node_heartbeat(self, node_id: str) -> None:
        """Update last_seen timestamp for a node."""
        await self._db.execute(
            "UPDATE nodes SET last_seen = ?, connected = 1 WHERE node_id = ?",
            (_now_iso(), node_id),
        )
        await self._db.commit()

    async def get_nodes(self) -> list[dict[str, Any]]:
        """Get all nodes (connected and disconnected)."""
        cursor = await self._db.execute(
            "SELECT * FROM nodes ORDER BY last_seen DESC"
        )
        rows = await cursor.fetchall()
        return [_row_to_dict(cursor, row) for row in rows]
