"""점검 필요 잠금의 알림 원장(카드 #1260).

요청의 점검 필요(lock)가 꺼짐에서 켜짐으로 바뀌는 **같은 트랜잭션**에 알림 한 줄을 적는다. 이 원장은
상태 정의 밖의 부수 기록이며, 사건과 전이는 바꾸지 않는다.

잠금을 켜는 여덟 사건(plan_failed_journal, attempt_failed, send_failed, lease_deadline, grace_deadline,
hello_missing, node_failed, late_failed)은 모두 `steps.step_request`의 잠금 비교 한 곳을 지나므로 적는 곳도
거기 하나다. 기동 때는 원장 줄이 없는데 잠긴 열린 요청을 채운다(`backfill_locked_requests`).
보내는 일은 `haniel_orch.inspection_notice_sender`가 맡는다.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import aiosqlite

from ..deploy_last_failure import last_failure
from .columns import LOCK_ON
from .inspection_notice_text import redact_secrets, render_notice_text

logger = logging.getLogger(__name__)

STATE_UNSENT = "unsent"
STATE_SENT = "sent"

BACKFILL_EVENT = "startup_backfill"

# 원장 줄이 없는데 잠겨 있는 열린 요청의 잎. 배포 중도 잠긴 채 실패 보고를 기다릴 수 있다.
LOCKED_OPEN_LEAVES = ("pending", "again", "retry", "deploying")

NOTICE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS inspection_notices (
    notice_id TEXT PRIMARY KEY,
    deploy_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    repo TEXT,
    branch TEXT,
    target_head TEXT,
    sm_status TEXT,
    event TEXT NOT NULL,
    failure_kind TEXT,
    attempt_id TEXT,
    reason TEXT,
    error TEXT,
    failed_at TEXT,
    prior_notice_count INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'unsent',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    next_attempt_at TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_inspection_notices_due
    ON inspection_notices(state, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_inspection_notices_deploy
    ON inspection_notices(deploy_id, created_at);
"""

_wake_listeners: set[Callable[[], None]] = set()


def register_wake_listener(listener: Callable[[], None]) -> None:
    _wake_listeners.add(listener)


def unregister_wake_listener(listener: Callable[[], None]) -> None:
    _wake_listeners.discard(listener)


def _wake() -> None:
    """커밋 직후 바로 보내도록 보내기 루프를 깨운다. 깨우기 실패가 원장 쓰기를 되돌리면 안 된다."""
    for listener in tuple(_wake_listeners):
        try:
            listener()
        except Exception:
            logger.exception("Inspection notice wake listener failed")


async def ensure_notice_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(NOTICE_SCHEMA_SQL)


async def record_notice(
    db: aiosqlite.Connection,
    *,
    deploy_id: str,
    event: str,
    now: str,
    node_id: str | None,
    repo: str | None,
    branch: str | None,
    target_head: str | None,
    sm_status: str | None,
) -> str:
    """잠금 전이 한 줄을 적는다. 호출부가 트랜잭션과 저장소 변경 잠금을 쥔다."""
    return await _insert_notice(
        db,
        deploy_id=deploy_id,
        event=event,
        now=now,
        node_id=node_id,
        repo=repo,
        branch=branch,
        target_head=target_head,
        sm_status=sm_status,
    )


async def _insert_notice(
    db: aiosqlite.Connection,
    *,
    deploy_id: str,
    event: str,
    now: str,
    node_id: str | None,
    repo: str | None,
    branch: str | None,
    target_head: str | None,
    sm_status: str | None,
) -> str:
    cursor = await db.execute(
        "SELECT COUNT(*) FROM inspection_notices WHERE deploy_id = ?", (deploy_id,)
    )
    prior_row = await cursor.fetchone()
    prior_notice_count = int(prior_row[0]) if prior_row is not None else 0
    failure = await last_failure(db, deploy_id)
    if failure is not None:
        # 원장 칸에도 가린 값을 적는다. 본문과 같은 값이라 원장을 그대로 읽어도 비밀값이 남지 않는다
        failure = dict(failure)
        if failure.get("reason") is not None:
            failure["reason"] = redact_secrets(str(failure["reason"]))
        if failure.get("error") is not None:
            failure["error"] = redact_secrets(str(failure["error"]))
    notice_id = f"inspection-{uuid.uuid4().hex}"
    text = render_notice_text(
        node_id=node_id,
        repo=repo,
        branch=branch,
        target_head=target_head,
        deploy_id=deploy_id,
        notice_id=notice_id,
        sm_status=sm_status,
        event=event,
        failure=failure,
        prior_notice_count=prior_notice_count,
    )
    await db.execute(
        """INSERT INTO inspection_notices
           (notice_id, deploy_id, node_id, repo, branch, target_head, sm_status, event,
            failure_kind, attempt_id, reason, error, failed_at, prior_notice_count, text,
            state, attempts, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
        (
            notice_id,
            deploy_id,
            node_id or "",
            repo,
            branch,
            target_head,
            sm_status,
            event,
            (failure or {}).get("kind"),
            (failure or {}).get("attempt_id"),
            (failure or {}).get("reason"),
            (failure or {}).get("error"),
            (failure or {}).get("at"),
            prior_notice_count,
            text,
            STATE_UNSENT,
            now,
        ),
    )
    _wake()
    return notice_id


async def backfill_locked_requests(db: aiosqlite.Connection, now: str) -> int:
    """기동 때 원장 줄이 없는데 잠긴 열린 요청마다 한 줄. 이미 줄이 있으면 아무것도 하지 않는다."""
    placeholders = ", ".join("?" for _ in LOCKED_OPEN_LEAVES)
    cursor = await db.execute(
        f"""SELECT e.deploy_id, e.node_id, e.repo, e.branch, e.target_head, e.sm_status
            FROM deploy_events e
            WHERE e.sm_lock = ?
              AND e.sm_status IN ({placeholders})
              AND NOT EXISTS (
                  SELECT 1 FROM inspection_notices n WHERE n.deploy_id = e.deploy_id
              )
            ORDER BY e.created_at, e.rowid""",
        (LOCK_ON, *LOCKED_OPEN_LEAVES),
    )
    rows = await cursor.fetchall()
    for deploy_id, node_id, repo, branch, target_head, sm_status in rows:
        await _insert_notice(
            db,
            deploy_id=deploy_id,
            event=BACKFILL_EVENT,
            now=now,
            node_id=node_id,
            repo=repo,
            branch=branch,
            target_head=target_head,
            sm_status=sm_status,
        )
    return len(rows)


async def due_unsent_notices(
    db: aiosqlite.Connection, now: str
) -> list[dict[str, Any]]:
    """보낼 차례가 된 보내지 않은 줄. 노드별 묶기는 보내는 쪽이 한다."""
    cursor = await db.execute(
        """SELECT * FROM inspection_notices
           WHERE state = ? AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
           ORDER BY created_at, rowid""",
        (STATE_UNSENT, now),
    )
    rows = await cursor.fetchall()
    names = [description[0] for description in cursor.description]
    return [dict(zip(names, row)) for row in rows]


async def mark_sent(
    db: aiosqlite.Connection, notice_ids: list[str], now: str
) -> None:
    if not notice_ids:
        return
    placeholders = ", ".join("?" for _ in notice_ids)
    await db.execute(
        f"""UPDATE inspection_notices
            SET state = ?, sent_at = ?, last_error = NULL, next_attempt_at = NULL
            WHERE notice_id IN ({placeholders}) AND state = ?""",
        (STATE_SENT, now, *notice_ids, STATE_UNSENT),
    )


def retry_delay_sec(attempts: int, base_sec: float, cap_sec: float) -> float:
    """첫 실패 1분, 그 뒤 두 배씩, 1시간에서 멈춘다."""
    return min(cap_sec, base_sec * (2 ** max(0, attempts - 1)))


async def record_failure(
    db: aiosqlite.Connection,
    notice_ids: list[str],
    *,
    error: str,
    now: datetime,
    base_sec: float,
    cap_sec: float,
) -> None:
    """보내기 실패를 시도 수와 다음 시각으로 적는다. 요청 상태와 잠금은 건드리지 않는다."""
    if not notice_ids:
        return
    placeholders = ", ".join("?" for _ in notice_ids)
    cursor = await db.execute(
        f"SELECT notice_id, attempts FROM inspection_notices WHERE notice_id IN ({placeholders})",
        notice_ids,
    )
    rows = await cursor.fetchall()
    for notice_id, attempts in rows:
        new_attempts = int(attempts) + 1
        delay = retry_delay_sec(new_attempts, base_sec, cap_sec)
        next_attempt_at = (now + timedelta(seconds=delay)).isoformat()
        await db.execute(
            """UPDATE inspection_notices
               SET attempts = ?, last_error = ?, next_attempt_at = ?
               WHERE notice_id = ? AND state = ?""",
            (new_attempts, error, next_attempt_at, notice_id, STATE_UNSENT),
        )


async def latest_notices_by_deploy(
    db: aiosqlite.Connection, deploy_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """요청마다 가장 나중 원장 줄(대기 목록의 inspection_notice 칸)."""
    unique_ids = list(dict.fromkeys(deploy_ids))
    if not unique_ids:
        return {}
    placeholders = ", ".join("?" for _ in unique_ids)
    cursor = await db.execute(
        f"""SELECT deploy_id, notice_id, state, sent_at, attempts, created_at
            FROM inspection_notices
            WHERE deploy_id IN ({placeholders})
            ORDER BY created_at, rowid""",
        unique_ids,
    )
    latest: dict[str, dict[str, Any]] = {}
    for deploy_id, notice_id, state, sent_at, attempts, created_at in await cursor.fetchall():
        latest[deploy_id] = {
            "deploy_id": deploy_id,
            "notice_id": notice_id,
            "state": state,
            "sent_at": sent_at,
            "attempts": attempts,
            "created_at": created_at,
        }
    return latest
