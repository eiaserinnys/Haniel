"""한 걸음 모듈: 네 표(deploy_events, deploy_attempts, deploy_retry_requirements,
deploy_retry_source_attempts)에 쓰는 SQL이 있는 유일한 곳(스키마 초기화 제외).

한 걸음은 저장소 변경 잠금과 트랜잭션 안에서(호출부가 쥔다) 이렇게 돈다.
1. 줄(잎 칸)과 걸음마다 읽는 사실을 읽는다.
2. `restore`로 새 인스턴스를 되살리고 사건 하나를 넣는다. 전이가 없으면 `NoTransition`이고
   줄에는 아무것도 쓰이지 않는다.
3. 결과 잎(요청 상태와 점검 필요)과 바깥 칸(status, reject_reason, outcome, 표시 줄의
   inspection_required 등)과 딸린 줄(표시 줄, 계보 줄)을 같은 트랜잭션에 쓴다. SQL의 `WHERE`는
   마지막 쓰기 재확인으로 남는다.

방송, 푸시, 타이머 같은 할 일은 커밋 뒤에 호출부가 돌린다(할 일 기호의 저장은 PR 2b).

500줄을 넘는다(모듈 크기 제한 예외): PR 1 때부터 넘었고 PR 2a는 점검 필요만 더했다. 믿음과 새 글을 더하는 PR 2b 전에 나누면 같은 자리를 두 번 옮기게 되어 뒤로 미룬다.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, NamedTuple

import aiosqlite

from .columns import (
    ATTEMPT_REGION,
    LOCK_OFF,
    LOCK_ON,
    LOCK_REGION,
    REQUEST_REGION,
    attempt_leaf_from_columns,
    attempt_leaf_is_consistent,
    lock_leaf_from_columns,
    request_leaf_from_columns,
    request_leaf_is_consistent,
)
from .definitions import (
    ATTEMPT_TERMINAL,
    REQUEST_CLOSED,
    REQUEST_OUTER_STATUS,
    AttemptEvidence,
    OrchRequest,
)
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


class AttemptStep(NamedTuple):
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


# ---------------------------------------------------------------- 시도 줄

_ATTEMPT_INSERT_COLUMNS = (
    "orchestrator_attempt_id",
    "deploy_id",
    "node_id",
    "repo",
    "branch",
    "target_head",
    "source",
    "execution_mode",
    "approved_by",
    "probe_id",
    "connection_generation",
    "proposal_fingerprint",
    "source_orchestrator_attempt_id",
    "journal_attempt_id",
    "manifest_identity",
    "manifest_digest",
    "journal_target_head",
    "journal_completed_at",
    "original_previous_head",
    "commits_json",
    "affected_services_json",
    "diff_stat",
    "detected_at",
    "started_at",
    "deadline_at",
)


async def create_attempt(db: aiosqlite.Connection, **values: Any) -> None:
    """시도 줄을 아직 증거가 없는 잎으로 만든다."""
    if set(values) != set(_ATTEMPT_INSERT_COLUMNS):
        raise KeyError(
            f"시도 줄 칸이 맞지 않는다: {sorted(set(values) ^ set(_ATTEMPT_INSERT_COLUMNS))}"
        )
    leaf = initial_row(AttemptEvidence)[ATTEMPT_REGION]
    names = (*_ATTEMPT_INSERT_COLUMNS, "sm_evidence")
    await db.execute(
        f"INSERT INTO deploy_attempts ({', '.join(names)}) "
        f"VALUES ({', '.join('?' for _ in names)})",
        (*(values[name] for name in _ATTEMPT_INSERT_COLUMNS), leaf),
    )


# fail 사건이 함께 쓰는 증거 칸(결과 글과 대조 글이 같은 걸음에 실패를 닫는다)
_FAIL_EXTRA_COLUMNS = frozenset(
    {
        "result_status",
        "result_error",
        "duration_ms",
        "settled_local_head",
        "settled_remote_head",
        "settled_at",
    }
)


def _attempt_columns(
    event: str, leaf: str, data: Mapping[str, Any], now: str
) -> dict[str, Any]:
    columns: dict[str, Any] = {}
    if event == "result_ok":
        columns.update(
            result_status="success", result_error=None, duration_ms=data["duration_ms"]
        )
    elif event in ("settled_ok", "settled_noted"):
        columns.update(
            settled_local_head=data["local_head"],
            settled_remote_head=data["remote_head"],
            settled_at=now,
        )
    elif event == "fail":
        columns.update(
            outcome="failed",
            terminal_kind=data["kind"],
            terminal_stage=data["stage"],
            terminal_reason=data["reason"],
            terminal_error=data["error"],
            completed_at=now,
        )
        extra = dict(data.get("extra") or {})
        unknown = set(extra) - _FAIL_EXTRA_COLUMNS
        if unknown:
            raise KeyError(f"fail 사건이 쓸 수 없는 칸: {sorted(unknown)}")
        columns.update(extra)
    elif event == "recovered":
        columns.update(outcome="evidence_recovered_success", completed_at=now)
    elif event == "superseded_by_node":
        columns.update(
            outcome="superseded",
            terminal_kind="node_deploy_succeeded",
            terminal_stage="node_deploy_report",
            terminal_reason="out_of_band_success",
            terminal_error="superseded by node-owned deployment",
            completed_at=now,
        )
    else:
        raise KeyError(f"시도 사건에 쓸 칸이 정해지지 않았다: {event}")
    if leaf == "ev_both" and event in ("result_ok", "settled_ok"):
        columns.update(outcome="success", completed_at=now)
    return columns


async def step_attempt(
    db: aiosqlite.Connection,
    orchestrator_attempt_id: str,
    event: str,
    *,
    facts: Mapping[str, bool] | None = None,
    data: Mapping[str, Any] | None = None,
    now: str | None = None,
) -> AttemptStep:
    """시도 줄에 사건 하나를 넣는다. 끝난 시도나 칸 없는 사건은 NoTransition."""
    data = data or {}
    now = now or now_iso()
    cursor = await db.execute(
        "SELECT outcome, sm_evidence, execution_mode FROM deploy_attempts "
        "WHERE orchestrator_attempt_id = ?",
        (orchestrator_attempt_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise NoTransition(
            AttemptEvidence.__name__,
            {"attempt": orchestrator_attempt_id},
            event,
            REASON_NO_ROW,
            orchestrator_attempt_id,
        )
    outcome, leaf, mode = row
    if leaf is None or not attempt_leaf_is_consistent(leaf, outcome):
        raise NoTransition(
            AttemptEvidence.__name__,
            {"outcome": outcome, "sm_evidence": leaf},
            event,
            REASON_STATE_MISMATCH,
            orchestrator_attempt_id,
        )
    given = {
        "execute_mode": mode == "execute",
        "recovery_mode": mode == "evidence_recovery",
        **dict(facts or {}),
    }
    try:
        new_leaf = advance(AttemptEvidence, {ATTEMPT_REGION: leaf}, event, given)[
            ATTEMPT_REGION
        ]
    except NoTransition as exc:
        exc.key = orchestrator_attempt_id
        raise
    columns = {
        "sm_evidence": new_leaf,
        **_attempt_columns(event, new_leaf, data, now),
    }
    assignments = ", ".join(f"{name} = ?" for name in columns)
    updated = await db.execute(
        f"UPDATE deploy_attempts SET {assignments} "
        "WHERE orchestrator_attempt_id = ? AND outcome = 'active' AND sm_evidence = ?",
        (*columns.values(), orchestrator_attempt_id, leaf),
    )
    if updated.rowcount != 1:
        raise NoTransition(
            AttemptEvidence.__name__,
            {"outcome": outcome, "sm_evidence": leaf},
            event,
            REASON_UPDATE_ZERO,
            orchestrator_attempt_id,
        )
    return AttemptStep(previous=leaf, leaf=new_leaf)


def attempt_is_terminal(leaf: str) -> bool:
    return leaf in ATTEMPT_TERMINAL


async def renew_deadline(
    db: aiosqlite.Connection,
    orchestrator_attempt_id: str,
    deadline_at: str,
    expected_budget_sec: int | None,
) -> None:
    """임대 늘림은 상태 칸이 아니라 데이터 칸이다(PR 2의 믿음 마감 자리)."""
    await db.execute(
        "UPDATE deploy_attempts SET deadline_at = ?, expected_budget_sec = ? "
        "WHERE orchestrator_attempt_id = ? AND outcome = 'active'",
        (deadline_at, expected_budget_sec, orchestrator_attempt_id),
    )


# ---------------------------------------------------------------- 기동 때 다시 계산


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
