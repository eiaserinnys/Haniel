"""Private unlocked node-registry mutations composed by :mod:`event_store`."""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def initialize_node_schema(db: aiosqlite.Connection) -> None:
    """Idempotently add generation ownership to pre-existing node tables."""
    cursor = await db.execute("PRAGMA table_info(nodes)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "connection_generation" not in columns:
        await db.execute("ALTER TABLE nodes ADD COLUMN connection_generation TEXT")
    if "connection_token" not in columns:
        await db.execute("ALTER TABLE nodes ADD COLUMN connection_token TEXT")


async def upsert_node(
    db: aiosqlite.Connection,
    node_id: str,
    hostname: str,
    os: str,
    arch: str,
    haniel_version: str,
    connected: bool,
    connection_generation: str | None,
    connection_token: str | None,
) -> None:
    now = _now_iso()
    await db.execute(
        """INSERT INTO nodes
           (node_id, hostname, os, arch, haniel_version, connected,
            connection_generation, connection_token, last_seen, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(node_id) DO UPDATE SET
             hostname = excluded.hostname,
             os = excluded.os,
             arch = excluded.arch,
             haniel_version = excluded.haniel_version,
             connected = excluded.connected,
             connection_generation = excluded.connection_generation,
             connection_token = excluded.connection_token,
             last_seen = excluded.last_seen""",
        (
            node_id,
            hostname,
            os,
            arch,
            haniel_version,
            int(connected),
            connection_generation,
            connection_token,
            now,
            now,
        ),
    )


async def update_node_heartbeat(
    db: aiosqlite.Connection,
    node_id: str,
    expected_generation: str | None,
    expected_connection_token: str | None,
) -> bool:
    if expected_generation is None or expected_connection_token is None:
        cursor = await db.execute(
            "UPDATE nodes SET last_seen = ?, connected = 1 WHERE node_id = ?",
            (_now_iso(), node_id),
        )
    else:
        cursor = await db.execute(
            "UPDATE nodes SET last_seen = ?, connected = 1 "
            "WHERE node_id = ? AND connection_generation = ? AND connection_token = ?",
            (_now_iso(), node_id, expected_generation, expected_connection_token),
        )
    return cursor.rowcount == 1


async def mark_node_disconnected(
    db: aiosqlite.Connection,
    node_id: str,
    expected_generation: str,
    expected_connection_token: str,
) -> bool:
    cursor = await db.execute(
        "UPDATE nodes SET connected = 0, last_seen = ? "
        "WHERE node_id = ? AND connection_generation = ? AND connection_token = ? "
        "AND connected = 1",
        (_now_iso(), node_id, expected_generation, expected_connection_token),
    )
    return cursor.rowcount == 1
