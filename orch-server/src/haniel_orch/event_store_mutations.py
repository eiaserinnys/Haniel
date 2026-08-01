"""Private unlocked deploy mutations composed by :mod:`event_store`."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
import aiosqlite

from .protocol import DeployStatus


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(cursor: aiosqlite.Cursor, row: tuple) -> dict[str, Any]:
    return {column[0]: value for column, value in zip(cursor.description, row)}


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


async def supersede_stale_pending_deploys(
    db: aiosqlite.Connection,
) -> list[str]:
    cursor = await db.execute(
        "SELECT deploy_id, node_id, repo, branch FROM deploy_events "
        "WHERE status = ? ORDER BY node_id, repo, branch, detected_at DESC, "
        "created_at DESC, deploy_id DESC",
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
        await _cleanup_retry_rows(db, [deploy_id])
        superseded.append(deploy_id)
    return superseded


async def supersede_pending_for_branch(
    db: aiosqlite.Connection,
    node_id: str,
    repo: str,
    branch: str,
) -> list[dict[str, Any]]:
    """Reject only pending canonicals older than the transaction-time latest."""
    latest_cursor = await db.execute(
        "SELECT deploy_id FROM deploy_events WHERE node_id = ? AND repo = ? "
        "AND branch = ? AND deploy_id NOT LIKE 'attempt:%' "
        "ORDER BY detected_at DESC, created_at DESC, deploy_id DESC LIMIT 1",
        (node_id, repo, branch),
    )
    latest = await latest_cursor.fetchone()
    if latest is None:
        return []
    latest_id = latest[0]
    cursor = await db.execute(
        "SELECT * FROM deploy_events WHERE node_id = ? AND repo = ? "
        "AND branch = ? AND status = ? AND deploy_id != ?",
        (node_id, repo, branch, DeployStatus.PENDING.value, latest_id),
    )
    rows = await cursor.fetchall()
    rejected: list[dict[str, Any]] = []
    now = _now_iso()
    for row in rows:
        item = _row_to_dict(cursor, row)
        item["commits"] = json.loads(item.pop("commits_json"))
        item["affected_services"] = json.loads(item.pop("affected_services_json"))
        item["reject_reason"] = f"superseded by {latest_id}"
        rejected.append(item)
        await db.execute(
            "UPDATE deploy_events SET status = ?, reject_reason = ?, updated_at = ? "
            "WHERE deploy_id = ? AND status = ?",
            (
                DeployStatus.REJECTED.value,
                item["reject_reason"],
                now,
                item["deploy_id"],
                DeployStatus.PENDING.value,
            ),
        )
    await _cleanup_retry_rows(db, [item["deploy_id"] for item in rejected])
    return rejected


async def reject_pending_deploys_for_nodes(
    db: aiosqlite.Connection,
    node_ids: list[str],
    reject_reason: str,
) -> list[dict[str, Any]]:
    placeholders = ", ".join("?" for _ in node_ids)
    cursor = await db.execute(
        f"SELECT * FROM deploy_events WHERE status = ? AND node_id IN ({placeholders})",
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
        await _cleanup_retry_rows(db, [item["deploy_id"] for item in rejected])
    return rejected


async def _cleanup_retry_rows(
    db: aiosqlite.Connection, deploy_ids: list[str]
) -> None:
    if not deploy_ids:
        return
    placeholders = ", ".join("?" for _ in deploy_ids)
    await db.execute(
        f"DELETE FROM deploy_retry_requirements WHERE deploy_id IN ({placeholders})",
        deploy_ids,
    )
    await db.execute(
        f"DELETE FROM deploy_retry_source_attempts WHERE deploy_id IN ({placeholders})",
        deploy_ids,
    )
    now = _now_iso()
    await db.execute(
        f"""UPDATE deploy_plan_probes
            SET status = 'terminal', terminal_kind = 'preflight_stale',
                terminal_stage = 'canonical_cleanup',
                terminal_reason = 'canonical_terminal',
                terminal_error = 'canonical became terminal during preflight',
                completed_at = ?
            WHERE deploy_id IN ({placeholders})
              AND status IN ('active','proposed')""",
        (now, *deploy_ids),
    )
