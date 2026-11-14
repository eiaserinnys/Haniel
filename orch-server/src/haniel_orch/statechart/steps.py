"""요청 줄의 한 걸음: 요청 표(deploy_events)와 딸린 표(deploy_retry_requirements,
deploy_retry_source_attempts)에 쓰는 SQL이 있는 유일한 곳(스키마 초기화 제외).
시도 줄(deploy_attempts)의 걸음은 `attempt_steps`에, 기동 때 다시 계산은 `backfill`에 있다.

한 걸음은 저장소 변경 잠금과 트랜잭션 안에서(호출부가 쥔다) 이렇게 돈다.
1. 줄(잎 칸 넷)과 걸음마다 읽는 사실을 읽는다.
2. `restore`로 새 인스턴스를 되살리고 사건 하나를 넣는다. 전이가 없으면 `NoTransition`이고
   줄에는 아무것도 쓰이지 않는다.
3. 결과 잎(요청 상태, 믿음, 점검 필요, 닫힌 뒤 적용 꼬리표)과 바깥 칸(status, reject_reason, 표시 줄의
   inspection_required, 꼬리표의 시각과 시도 번호 등)과 딸린 줄(표시 줄, 계보 줄)을 같은 트랜잭션에 쓴다.
   SQL의 `WHERE`는 마지막 쓰기 재확인으로 남는다.
4. 정의가 낸 할 일 기호를 `RequestStep.todo`로 돌려준다. 기호는 DB에 적지 않는다(2b-1 8항).

방송, 푸시, 타이머, 허가와 탐침 보내기, 거절 답 같은 할 일은 커밋 뒤에 호출부가 기호를 보고 돌린다.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, NamedTuple

import aiosqlite

from .columns import BELIEF_REGION, LOCK_ON, LOCK_REGION, REQUEST_REGION, TAG_REGION
from .definitions import REQUEST_CLOSED, REQUEST_OUTER_STATUS, OrchRequest
from .restore import (
    REASON_NO_ROW,
    REASON_STATE_MISMATCH,
    REASON_UPDATE_ZERO,
    NoTransition,
    advance_with_todo,
    initial_row,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RequestStep(NamedTuple):
    previous: str
    leaf: str
    todo: tuple[str, ...] = ()
    belief_previous: str | None = None
    belief: str | None = None


# 시도를 닫아 대기(again, retry)나 밀림으로 보내는 사건. 닫힌 시도는 언제나 계보에 더한다(design.md:165)
_CLOSING_EVENTS = frozenset(
    {
        "attempt_failed",
        "send_failed",
        "late_failed",
        "declined",
        "ack_deadline",
        "lease_deadline",
        "grace_deadline",
        "hello_missing",
        "plan_failed_journal",
    }
)

# ---------------------------------------------------------------- 요청 줄

_INSERT_REQUEST = """INSERT OR IGNORE INTO deploy_events
    (deploy_id, node_id, repo, branch, status, sm_status, sm_belief, sm_lock, sm_tag,
     commits_json, affected_services_json, diff_stat, detected_at,
     created_at, updated_at, target_head, deployment_kind,
     expected_manifest_identity, expected_manifest_digest, is_self_update)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""


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
    """요청 줄을 처음 대기로 만든다. 같은 deploy_id가 있으면 아무것도 쓰지 않고 False.

    요청 줄이 생기기 전에 노드가 스스로 한 배포의 실패가 이미 적혀 있으면(G12, wire-v1 6절) 같은 걸음에서
    node_failed를 넣어 점검 필요가 켜진 채 시작한다. 그 까닭은 노드 시도 줄(이력과 대기 목록의 직전 실패)에 보인다.
    """
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
            initial[BELIEF_REGION],
            initial[LOCK_REGION],
            initial[TAG_REGION],
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
    if cursor.rowcount <= 0:
        return False
    prior = await db.execute(
        "SELECT 1 FROM node_deploy_attempts WHERE deploy_id = ? AND outcome = 'failed' LIMIT 1",
        (deploy_id,),
    )
    if await prior.fetchone() is not None:
        await step_request(
            db, deploy_id, "node_failed", facts={"no_active_attempt": True}, now=now
        )
    return True


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
    if event in (
        "attempt_failed",
        "send_failed",
        "declined",
        "ack_deadline",
        "lease_deadline",
        "grace_deadline",
        "hello_missing",
    ):
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
    if event == "success_evidence":
        # 열린 대기가 늦은 성공으로 닫힌다(꼬리표만 바뀌는 닫힌 요청은 여기 오지 않는다)
        return {"error": None, "duration_ms": data.get("duration_ms")}
    if event in ("late_failed", "plan_failed_journal"):
        return {}
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
   VALUES (?, ?, ?, ?, ?, ?)
   ON CONFLICT(deploy_id) DO UPDATE SET
     source_orchestrator_attempt_id=excluded.source_orchestrator_attempt_id,
     last_failed_orchestrator_attempt_id=excluded.last_failed_orchestrator_attempt_id,
     updated_at=excluded.updated_at,
     inspection_required=excluded.inspection_required"""


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
        "SELECT status, sm_status, sm_belief, sm_lock, sm_tag FROM deploy_events WHERE deploy_id = ?",
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
    status, leaf, belief, lock, tag = row
    before = {
        "status": status,
        "sm_status": leaf,
        "sm_belief": belief,
        "sm_lock": lock,
        "sm_tag": tag,
    }
    if (
        leaf is None
        or belief is None
        or lock is None
        or tag is None
        or REQUEST_OUTER_STATUS.get(leaf) != status
    ):
        raise NoTransition(
            OrchRequest.__name__, before, event, REASON_STATE_MISMATCH, deploy_id
        )
    try:
        after, todo = advance_with_todo(
            OrchRequest,
            {
                REQUEST_REGION: leaf,
                BELIEF_REGION: belief,
                LOCK_REGION: lock,
                TAG_REGION: tag,
            },
            event,
            dict(facts or {}),
        )
    except NoTransition as exc:
        exc.key = deploy_id
        raise
    new_leaf = after[REQUEST_REGION]
    new_belief = after[BELIEF_REGION]
    new_lock = after[LOCK_REGION]
    new_tag = after[TAG_REGION]
    moved = new_leaf != leaf
    lock_changed = new_lock != lock
    tag_changed = new_tag != tag
    result = RequestStep(
        previous=leaf,
        leaf=new_leaf,
        todo=tuple(todo),
        belief_previous=belief,
        belief=new_belief,
    )

    # 요청 상태가 옮겨질 때만 바깥 칸과 updated_at을 쓴다. 믿음이나 점검 필요나 꼬리표만 바뀌면(받았다, 시작했다,
    # 닫힌 요청에 닿은 시도 실패, 도는 시도가 있는 배포 중의 노드 실패 보고, 닫힌 요청에 닿은 성공 증거) 요청 줄의
    # 다른 칸은 건드리지 않는다. 제자리 전이만 있는 걸음(자동 요청의 무시, 끊김)은 아무 칸도 쓰지 않는다
    columns: dict[str, Any] = {}
    if moved:
        columns = {
            "status": REQUEST_OUTER_STATUS[new_leaf],
            "sm_status": new_leaf,
            **_request_columns(event, leaf, new_leaf, data),
            "updated_at": now,
        }
    if new_belief != belief:
        columns["sm_belief"] = new_belief
    if lock_changed:
        columns["sm_lock"] = new_lock
    if tag_changed:
        # 꼬리표의 바깥 칸은 선 시각과 그때의 시도 번호(노드가 스스로 한 배포면 노드 시도 번호)다
        columns["sm_tag"] = new_tag
        columns["applied_after_close_at"] = now
        columns["applied_after_close_attempt_id"] = data["attempt_id"]
    if not columns:
        return result
    assignments = ", ".join(f"{name} = ?" for name in columns)
    updated = await db.execute(
        f"UPDATE deploy_events SET {assignments} "
        "WHERE deploy_id = ? AND status = ? AND sm_status = ? AND sm_belief = ? AND sm_lock = ? AND sm_tag = ?",
        (*columns.values(), deploy_id, status, leaf, belief, lock, tag),
    )
    if updated.rowcount != 1:
        raise NoTransition(
            OrchRequest.__name__, before, event, REASON_UPDATE_ZERO, deploy_id
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
        return result

    if event in _CLOSING_EVENTS and moved:
        # 닫힌 시도는 언제나 계보에 더한다. source는 호출부가 정한다(보내기 실패와 「실행하지 않음」 답은 옮기지 않는다.
        # 처음 대기에 닿은 늦은 실패와 실패 저널은 실행한 그 시도를 source로 둔다). 표시 줄의 inspection_required는
        # 점검 필요 잎을 따른다(다시 대기는 0, 실패 뒤 대기는 1)
        await db.execute(
            _UPSERT_MARKER,
            (
                deploy_id,
                data["stable_source"],
                data["failed_attempt_id"],
                data["marker_created_at"] or now,
                now,
                1 if new_lock == LOCK_ON else 0,
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
    return result


async def set_postponed_until(
    db: aiosqlite.Connection, deploy_id: str, postponed_until: str, now: str
) -> None:
    """보류 시한은 상태 칸이 아니라 데이터 칸이다. 잎은 건드리지 않는다."""
    await db.execute(
        "UPDATE deploy_events SET postponed_until = ?, updated_at = ? "
        "WHERE deploy_id = ?",
        (postponed_until, now, deploy_id),
    )
