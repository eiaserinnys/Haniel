"""Private unlocked deploy mutations composed by :mod:`event_store`."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import aiosqlite

from .protocol import DeployStatus


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(cursor: aiosqlite.Cursor, row: tuple) -> dict[str, Any]:
    return {column[0]: value for column, value in zip(cursor.description, row)}


async def reopen_terminal_deploy(
    db: aiosqlite.Connection,
    deploy_id: str,
    expected_status: DeployStatus,
) -> bool:
    cursor = await db.execute(
        "SELECT * FROM deploy_events WHERE deploy_id = ? AND status = ?",
        (deploy_id, expected_status.value),
    )
    row = await cursor.fetchone()
    if row is None:
        return False

    event = _row_to_dict(cursor, row)
    attempt_id = f"attempt:{uuid4().hex}"
    terminal_at = event["updated_at"]
    await db.execute(
        """INSERT INTO deploy_events
           (deploy_id, node_id, repo, branch, status, commits_json,
            affected_services_json, diff_stat, detected_at, approved_by,
            reject_reason, error, duration_ms, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            attempt_id,
            event["node_id"],
            event["repo"],
            event["branch"],
            event["status"],
            event["commits_json"],
            event["affected_services_json"],
            event["diff_stat"],
            event["detected_at"],
            event["approved_by"],
            event["reject_reason"],
            event["error"],
            event["duration_ms"],
            terminal_at,
            terminal_at,
        ),
    )
    now = _now_iso()
    updated = await db.execute(
        """UPDATE deploy_events
           SET status = ?, approved_by = NULL, reject_reason = NULL,
               error = NULL, duration_ms = NULL,
               created_at = ?, updated_at = ?
           WHERE deploy_id = ? AND status = ?""",
        (
            DeployStatus.PENDING.value,
            now,
            now,
            deploy_id,
            expected_status.value,
        ),
    )
    if updated.rowcount != 1:
        raise RuntimeError("terminal deploy changed during reopen")
    return True


async def apply_deploy_result(
    db: aiosqlite.Connection,
    deploy_id: str,
    status: DeployStatus,
    error: str | None,
    duration_ms: int | None,
) -> bool:
    updates = ["status = ?", "updated_at = ?", "error = ?"]
    params: list[Any] = [status.value, _now_iso(), error]
    if duration_ms is not None:
        updates.append("duration_ms = ?")
        params.append(duration_ms)
    params.extend([deploy_id, DeployStatus.DEPLOYING.value])
    cursor = await db.execute(
        f"UPDATE deploy_events SET {', '.join(updates)} "
        "WHERE deploy_id = ? AND status = ?",
        params,
    )
    return cursor.rowcount == 1


async def transition_deploy_status(
    db: aiosqlite.Connection,
    deploy_id: str,
    expected: DeployStatus,
    target: DeployStatus,
) -> bool:
    cursor = await db.execute(
        "UPDATE deploy_events SET status = ?, updated_at = ? "
        "WHERE deploy_id = ? AND status = ?",
        (target.value, _now_iso(), deploy_id, expected.value),
    )
    return cursor.rowcount == 1


async def resolve_pending_branch(
    db: aiosqlite.Connection,
    node_id: str,
    repo: str,
    branch: str,
) -> list[str]:
    cursor = await db.execute(
        "SELECT deploy_id FROM deploy_events WHERE node_id = ? AND repo = ? "
        "AND branch = ? AND status = ?",
        (node_id, repo, branch, DeployStatus.PENDING.value),
    )
    deploy_ids = [row[0] for row in await cursor.fetchall()]
    if deploy_ids:
        placeholders = ", ".join("?" for _ in deploy_ids)
        await db.execute(
            f"UPDATE deploy_events SET status = ?, updated_at = ? "
            f"WHERE deploy_id IN ({placeholders}) AND status = ?",
            (
                DeployStatus.SUCCESS.value,
                _now_iso(),
                *deploy_ids,
                DeployStatus.PENDING.value,
            ),
        )
    return deploy_ids


async def supersede_stale_pending_deploys(
    db: aiosqlite.Connection,
) -> list[str]:
    cursor = await db.execute(
        "SELECT deploy_id, node_id, repo, branch FROM deploy_events "
        "WHERE status = ? ORDER BY node_id, repo, branch, created_at DESC, "
        "detected_at DESC, deploy_id DESC",
        (DeployStatus.PENDING.value,),
    )
    kept_by_group: dict[tuple[str, str, str], str] = {}
    superseded: list[str] = []
    now = _now_iso()
    for deploy_id, node_id, repo, branch in await cursor.fetchall():
        key = (node_id, repo, branch)
        kept = kept_by_group.get(key)
        if kept is None:
            kept_by_group[key] = deploy_id
            continue
        await db.execute(
            "UPDATE deploy_events SET status = ?, reject_reason = ?, "
            "updated_at = ? WHERE deploy_id = ?",
            (
                DeployStatus.REJECTED.value,
                f"superseded by {kept}",
                now,
                deploy_id,
            ),
        )
        superseded.append(deploy_id)
    return superseded


async def reject_pending_deploys_for_nodes(
    db: aiosqlite.Connection,
    node_ids: list[str],
    reject_reason: str,
) -> list[dict[str, Any]]:
    placeholders = ", ".join("?" for _ in node_ids)
    cursor = await db.execute(
        f"SELECT * FROM deploy_events WHERE status = ? "
        f"AND node_id IN ({placeholders})",
        (DeployStatus.PENDING.value, *node_ids),
    )
    rejected = []
    for row in await cursor.fetchall():
        item = _row_to_dict(cursor, row)
        item["commits"] = json.loads(item.pop("commits_json"))
        item["affected_services"] = json.loads(item.pop("affected_services_json"))
        rejected.append(item)
    if rejected:
        await db.execute(
            f"UPDATE deploy_events SET status = ?, reject_reason = ?, "
            f"updated_at = ? WHERE status = ? AND node_id IN ({placeholders})",
            (
                DeployStatus.REJECTED.value,
                reject_reason,
                _now_iso(),
                DeployStatus.PENDING.value,
                *node_ids,
            ),
        )
    return rejected
