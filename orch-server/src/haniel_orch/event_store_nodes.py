"""Private unlocked node-registry mutations composed by :mod:`event_store`."""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def upsert_node(
    db: aiosqlite.Connection,
    node_id: str,
    hostname: str,
    os: str,
    arch: str,
    haniel_version: str,
    connected: bool,
) -> None:
    now = _now_iso()
    await db.execute(
        """INSERT OR REPLACE INTO nodes
           (node_id, hostname, os, arch, haniel_version, connected,
            last_seen, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (node_id, hostname, os, arch, haniel_version, int(connected), now, now),
    )


async def update_node_heartbeat(db: aiosqlite.Connection, node_id: str) -> None:
    await db.execute(
        "UPDATE nodes SET last_seen = ?, connected = 1 WHERE node_id = ?",
        (_now_iso(), node_id),
    )


async def mark_node_disconnected(db: aiosqlite.Connection, node_id: str) -> None:
    await db.execute(
        "UPDATE nodes SET connected = 0, last_seen = ? WHERE node_id = ?",
        (_now_iso(), node_id),
    )
