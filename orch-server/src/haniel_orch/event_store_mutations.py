"""Private unlocked deploy mutations composed by :mod:`event_store`.

요청 줄의 상태를 바꾸는 쓰기는 모두 `statechart.steps.step_request`를 거친다.
여기서는 어느 줄에 사건을 넣을지 고르고, 계획 묻기 줄(정의 밖 저장)을 닫는다.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
import aiosqlite

from .protocol import DeployStatus
from .statechart import steps
from .statechart.columns import LEAF_COLUMNS
from .statechart.restore import NoTransition


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(cursor: aiosqlite.Cursor, row: tuple) -> dict[str, Any]:
    return {
        column[0]: value
        for column, value in zip(cursor.description, row)
        if column[0] not in LEAF_COLUMNS
    }


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
        await steps.step_request(
            db,
            deploy_id,
            "newer_commit",
            data={"reason": f"superseded by {kept}"},
            now=now,
        )
        await _close_probes(db, [deploy_id])
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
        await steps.step_request(
            db,
            item["deploy_id"],
            "newer_commit",
            data={"reason": item["reject_reason"]},
            now=now,
        )
    await _close_probes(db, [item["deploy_id"] for item in rejected])
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
        now = _now_iso()
        for item in rejected:
            await steps.step_request(
                db,
                item["deploy_id"],
                "reject",
                data={"reason": reject_reason},
                now=now,
            )
        await _close_probes(db, [item["deploy_id"] for item in rejected])
    return rejected


async def close_absent_node_requests(
    db: aiosqlite.Connection,
    *,
    absent_before: str,
    connected_node_ids: set[str],
    now: str,
) -> list[dict[str, Any]]:
    """기한 넘게 연결되지 않은 노드의 대기 요청(처음 대기, 다시 대기, 실패 뒤 대기)을 거절 모양으로 닫는다(어긋남 7).

    기준 시각은 nodes.last_seen이고, nodes 표에 없는 노드의 요청은 그 요청의 created_at이다. 지금 연결된 노드의
    요청은 내리지 않는다. 배포 중은 믿음별 마감이 닫으므로 여기서 다루지 않는다.
    """
    cursor = await db.execute(
        "SELECT e.deploy_id, e.node_id, e.created_at, n.last_seen "
        "FROM deploy_events e LEFT JOIN nodes n ON n.node_id = e.node_id "
        "WHERE e.sm_status IN ('pending', 'again', 'retry')"
    )
    closed: list[dict[str, Any]] = []
    for deploy_id, node_id, created_at, last_seen in await cursor.fetchall():
        if node_id in connected_node_ids:
            continue
        seen_at = last_seen or created_at
        if seen_at >= absent_before:
            continue
        reason = f"node absent since {seen_at}"
        try:
            await steps.step_request(
                db, deploy_id, "node_absent", data={"reason": reason}, now=now
            )
        except NoTransition as exc:
            exc.warn("node_absent")
            continue
        closed.append(
            {"deploy_id": deploy_id, "node_id": node_id, "reject_reason": reason}
        )
    await _close_probes(db, [item["deploy_id"] for item in closed])
    return closed


async def _close_probes(db: aiosqlite.Connection, deploy_ids: list[str]) -> None:
    """닫힌 요청의 열린 계획 묻기를 닫는다(계획 묻기 줄은 정의 밖 저장)."""
    if not deploy_ids:
        return
    placeholders = ", ".join("?" for _ in deploy_ids)
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
