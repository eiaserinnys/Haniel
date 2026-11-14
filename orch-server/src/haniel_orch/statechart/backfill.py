"""기동 때 다시 계산: 잎이 비었거나 바깥 칸과 어긋난 줄만 옛 칸에서 다시 계산한다."""

from __future__ import annotations

import aiosqlite

from .columns import (
    LOCK_OFF,
    LOCK_ON,
    attempt_leaf_from_columns,
    attempt_leaf_is_consistent,
    lock_leaf_from_columns,
    request_leaf_from_columns,
    request_leaf_is_consistent,
)


async def backfill_leaves(db: aiosqlite.Connection) -> dict[str, int]:
    """잎이 비었거나 바깥 칸과 어긋난 줄만 옛 칸에서 다시 계산한다.

    되돌렸다 다시 올려 옛 코드가 바깥 칸만 고쳐 둔 줄도 이것으로 맞춰진다.
    정의에 잎이 없는 옛 값(approved, failed)은 요청 상태 잎을 비워 둔다.
    점검 필요 잎은 비어 있는 줄에만 채운다. 어긋남 고침은 한 방향뿐이다: 대기(pending, retry)인데
    표시 줄이 있고 점검 필요가 꺼져 있으면 켠다(PR 1 코드로 되돌렸다 다시 올린 줄). 그 밖의 켜짐은 내리지 않는다.
    """
    fixed = {"requests": 0, "locks": 0, "attempts": 0}
    cursor = await db.execute(
        """SELECT e.deploy_id, e.status, e.reject_reason, e.sm_status, e.sm_lock,
                  EXISTS (SELECT 1 FROM deploy_retry_requirements r
                          WHERE r.deploy_id = e.deploy_id),
                  (SELECT r.inspection_required FROM deploy_retry_requirements r
                   WHERE r.deploy_id = e.deploy_id)
           FROM deploy_events e"""
    )
    for deploy_id, status, reason, leaf, lock, marked, inspection in await cursor.fetchall():
        if not request_leaf_is_consistent(leaf, status, bool(marked)):
            expected = request_leaf_from_columns(status, reason, bool(marked))
            if expected != leaf:
                await db.execute(
                    "UPDATE deploy_events SET sm_status = ? WHERE deploy_id = ?",
                    (expected, deploy_id),
                )
                fixed["requests"] += 1
        if lock not in (LOCK_ON, LOCK_OFF):
            await db.execute(
                "UPDATE deploy_events SET sm_lock = ? WHERE deploy_id = ?",
                (
                    lock_leaf_from_columns(status, bool(marked), bool(inspection)),
                    deploy_id,
                ),
            )
            fixed["locks"] += 1
        elif status == "pending" and marked and lock == LOCK_OFF:
            await db.execute(
                "UPDATE deploy_events SET sm_lock = ? WHERE deploy_id = ?",
                (LOCK_ON, deploy_id),
            )
            await db.execute(
                "UPDATE deploy_retry_requirements SET inspection_required = 1 "
                "WHERE deploy_id = ?",
                (deploy_id,),
            )
            fixed["locks"] += 1
    cursor = await db.execute(
        "SELECT orchestrator_attempt_id, outcome, result_status, settled_at, "
        "sm_evidence FROM deploy_attempts"
    )
    for attempt_id, outcome, result_status, settled_at, leaf in await cursor.fetchall():
        if attempt_leaf_is_consistent(leaf, outcome):
            continue
        expected = attempt_leaf_from_columns(outcome, result_status, settled_at)
        if expected == leaf:
            continue
        await db.execute(
            "UPDATE deploy_attempts SET sm_evidence = ? "
            "WHERE orchestrator_attempt_id = ?",
            (expected, attempt_id),
        )
        fixed["attempts"] += 1
    return fixed
