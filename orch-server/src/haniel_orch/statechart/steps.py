"""요청 줄의 한 걸음: 요청 표(deploy_events)와 딸린 표(deploy_retry_requirements,
deploy_retry_source_attempts)에 쓰는 SQL이 있는 유일한 곳(스키마 초기화 제외).
시도 줄(deploy_attempts)의 걸음은 `attempt_steps`에, 기동 때 다시 계산은 `backfill`에 있다.

한 걸음은 저장소 변경 잠금과 트랜잭션 안에서(호출부가 쥔다) 이렇게 돈다.
1. 줄(잎 칸)과 걸음마다 읽는 사실을 읽는다.
2. `restore`로 새 인스턴스를 되살리고 사건 하나를 넣는다. 전이가 없으면 `NoTransition`이고
   줄에는 아무것도 쓰이지 않는다.
3. 결과 잎(요청 상태와 점검 필요)과 바깥 칸(status, reject_reason, outcome, 표시 줄의
   inspection_required 등)과 딸린 줄(표시 줄, 계보 줄)을 같은 트랜잭션에 쓴다. SQL의 `WHERE`는
   마지막 쓰기 재확인으로 남는다.

방송, 푸시, 타이머 같은 할 일은 커밋 뒤에 호출부가 돌린다(할 일 기호의 저장은 PR 2b).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, NamedTuple

import aiosqlite

from .columns import LOCK_ON, LOCK_REGION, REQUEST_REGION
from .definitions import REQUEST_CLOSED, REQUEST_OUTER_STATUS, OrchRequest
from .restore import (
    REASON_NO_ROW,
    REASON_STATE_MISMATCH,
    REASON_UPDATE_ZERO,
    NoTransition,
    advance,
    initial_row,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RequestStep(NamedTuple):
    previous: str
    leaf: str


# ---------------------------------------------------------------- 요청 줄

_INSERT_REQUEST = """INSERT OR IGNORE INTO deploy_events
    (deploy_id, node_id, repo, branch, status, sm_status, sm_lock,
     commits_json, affected_services_json, diff_stat, detected_at,
     created_at, updated_at, target_head, deployment_kind,
     expected_manifest_identity, expected_manifest_digest, is_self_update)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""


async def create_request(
    db: aiosqlite.Connection,
    *,
    deploy_id: str,
    node_id: str,
    repo: str,
    branch: str,
    commits_json: str,
    affected_services_json: str,
    diff_stat: str | None,
    detected_at: str,
    target_head: str,
    deployment_kind: str,
    expected_manifest_identity: str | None,
    expected_manifest_digest: str | None,
    is_self_update: bool,
    now: str,
) -> bool:
    """요청 줄을 처음 대기로 만든다. 같은 deploy_id가 있으면 아무것도 쓰지 않고 False."""
    initial = initial_row(OrchRequest)
    leaf = initial[REQUEST_REGION]
    cursor = await db.execute(
        _INSERT_REQUEST,
        (
            deploy_id,
            node_id,
            repo,
            branch,
            REQUEST_OUTER_STATUS[leaf],
            leaf,
            initial[LOCK_REGION],
            commits_json,
            affected_services_json,
            diff_stat,
            detected_at,
            now,
            now,
            target_head,
            deployment_kind,
            expected_manifest_identity,
            expected_manifest_digest,
            int(is_self_update),
        ),
    )
    return cursor.rowcount > 0


def _request_columns(
    event: str, previous: str, leaf: str, data: Mapping[str, Any]
) -> dict[str, Any]:
    """사건과 출발, 결과 잎에서 요청 상태가 옮겨질 때 바깥 칸에 쓸 값을 정한다."""
    if event == "newer_commit":
        return {"reject_reason": data["reason"]}
    if event == "reject":
        reason = data.get("reason")
        return {} if reason is None else {"reject_reason": reason}
    if event == "begin_attempt":
        return {"approved_by": data["approved_by"]}
    if event == "attempt_succeeded":
        return {"error": None, "duration_ms": data.get("duration_ms")}
    if event in ("attempt_failed", "send_failed"):
        if leaf == "superseded":
            return {"reject_reason": "superseded by newer canonical"}
        return {"approved_by": None, "error": None, "duration_ms": None}
    if event == "node_succeeded":
        return {
            "approved_by": None,
            "error": None,
            "duration_ms": data.get("duration_ms"),
        }
    if event == "node_failed":
        if previous != "deploying":
            return {}
        return {"approved_by": None, "error": None, "duration_ms": None}
    if event in ("observed", "observed_self_update"):
        return {}
    if event == "reopen":
        return {
            "reject_reason": None,
            "postponed_until": None,
            "approved_by": None,
            "error": None,
        }
    raise KeyError(f"요청 사건에 쓸 칸이 정해지지 않았다: {event}")


_UPSERT_MARKER = """INSERT INTO deploy_retry_requirements
   (deploy_id, source_orchestrator_attempt_id, last_failed_orchestrator_attempt_id,
    created_at, updated_at, inspection_required)
   VALUES (?, ?, ?, ?, ?, 1)
   ON CONFLICT(deploy_id) DO UPDATE SET
     source_orchestrator_attempt_id=excluded.source_orchestrator_attempt_id,
     last_failed_orchestrator_attempt_id=excluded.last_failed_orchestrator_attempt_id,
     updated_at=excluded.updated_at,
     inspection_required=1"""


async def step_request(
    db: aiosqlite.Connection,
    deploy_id: str,
    event: str,
    *,
    facts: Mapping[str, bool] | None = None,
    data: Mapping[str, Any] | None = None,
    now: str | None = None,
) -> RequestStep:
    """요청 줄에 사건 하나를 넣는다. 전이가 없으면 NoTransition(줄은 그대로)."""
    data = data or {}
    now = now or now_iso()
    cursor = await db.execute(
        "SELECT status, sm_status, sm_lock FROM deploy_events WHERE deploy_id = ?",
        (deploy_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise NoTransition(
            OrchRequest.__name__,
            {"deploy_id": deploy_id},
            event,
            REASON_NO_ROW,
            deploy_id,
        )
    status, leaf, lock = row
    if leaf is None or lock is None or REQUEST_OUTER_STATUS.get(leaf) != status:
        raise NoTransition(
            OrchRequest.__name__,
            {"status": status, "sm_status": leaf, "sm_lock": lock},
            event,
            REASON_STATE_MISMATCH,
            deploy_id,
        )
    try:
        after = advance(
            OrchRequest,
            {REQUEST_REGION: leaf, LOCK_REGION: lock},
            event,
            dict(facts or {}),
        )
    except NoTransition as exc:
        exc.key = deploy_id
        raise
    new_leaf, new_lock = after[REQUEST_REGION], after[LOCK_REGION]
    moved = new_leaf != leaf
    lock_changed = new_lock != lock

    # 요청 상태가 옮겨질 때만 바깥 칸과 updated_at을 쓴다. 점검 필요만 바뀌면(닫힌 요청에 닿은 시도 실패,
    # 도는 시도가 있는 배포 중의 노드 실패 보고) 요청 줄의 다른 칸은 건드리지 않는다
    columns: dict[str, Any] = {}
    if moved:
        columns = {
            "status": REQUEST_OUTER_STATUS[new_leaf],
            "sm_status": new_leaf,
            **_request_columns(event, leaf, new_leaf, data),
            "updated_at": now,
        }
    if lock_changed:
        columns["sm_lock"] = new_lock
    assignments = ", ".join(f"{name} = ?" for name in columns)
    updated = await db.execute(
        f"UPDATE deploy_events SET {assignments} "
        "WHERE deploy_id = ? AND status = ? AND sm_status = ? AND sm_lock = ?",
        (*columns.values(), deploy_id, status, leaf, lock),
    )
    if updated.rowcount != 1:
        raise NoTransition(
            OrchRequest.__name__,
            {"status": status, "sm_status": leaf, "sm_lock": lock},
            event,
            REASON_UPDATE_ZERO,
            deploy_id,
        )

    if new_leaf in REQUEST_CLOSED:
        if moved:
            await db.execute(
                "DELETE FROM deploy_retry_requirements WHERE deploy_id = ?",
                (deploy_id,),
            )
            await db.execute(
                "DELETE FROM deploy_retry_source_attempts WHERE deploy_id = ?",
                (deploy_id,),
            )
        return RequestStep(previous=leaf, leaf=new_leaf)

    if event in ("attempt_failed", "send_failed") and moved:
        # 닫힌 시도는 언제나 계보에 더한다. source는 호출부가 정한다(보내기 실패는 옮기지 않는다)
        await db.execute(
            _UPSERT_MARKER,
            (
                deploy_id,
                data["stable_source"],
                data["failed_attempt_id"],
                data["marker_created_at"] or now,
                now,
            ),
        )
        await db.execute(
            "INSERT OR IGNORE INTO deploy_retry_source_attempts VALUES (?, ?, ?)",
            (deploy_id, data["failed_attempt_id"], now),
        )
    elif event == "node_failed" or (event == "reopen" and new_leaf == "retry"):
        # 노드가 스스로 한 배포의 실패와 잠긴 채 되살아난 줄은 두 시도 번호 칸이 빈 표시 줄을 만든다
        await db.execute(
            "INSERT OR IGNORE INTO deploy_retry_requirements "
            "(deploy_id, created_at, updated_at, inspection_required) "
            "VALUES (?, ?, ?, 1)",
            (deploy_id, now, now),
        )
    if lock_changed:
        # 점검 필요의 바깥 칸은 표시 줄의 inspection_required다. 잎과 같은 걸음에 맞춘다
        await db.execute(
            "UPDATE deploy_retry_requirements SET inspection_required = ?, updated_at = ? "
            "WHERE deploy_id = ?",
            (1 if new_lock == LOCK_ON else 0, now, deploy_id),
        )
    return RequestStep(previous=leaf, leaf=new_leaf)


async def set_postponed_until(
    db: aiosqlite.Connection, deploy_id: str, postponed_until: str, now: str
) -> None:
    """보류 시한은 상태 칸이 아니라 데이터 칸이다. 잎은 건드리지 않는다."""
    await db.execute(
        "UPDATE deploy_events SET postponed_until = ?, updated_at = ? "
        "WHERE deploy_id = ?",
        (postponed_until, now, deploy_id),
    )
