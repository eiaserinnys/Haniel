"""알림 원장의 저장소 접근(카드 #1260).

`EventStore`의 공개 메서드로 노출한다. 쓰기는 저장소 변경 잠금과 트랜잭션 안에서 하고, 보내는 쪽은
이 메서드만 쓴다(원장 SQL은 `statechart.inspection_notice` 한 곳에 있다).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from .event_store_rows import now_iso
from .statechart import inspection_notice


class InspectionNoticeStoreMixin:
    """EventStore에 섞는 점검 알림 원장 메서드."""

    _db: aiosqlite.Connection | None
    _mutation_lock: asyncio.Lock

    async def record_startup_inspection_notices(self) -> int:
        """기동 때 원장 줄이 없는 잠긴 열린 요청을 채운다. 채운 줄 수를 돌려준다."""
        async with self._mutation_lock:
            db = self._db
            assert db is not None
            try:
                count = await inspection_notice.backfill_locked_requests(db, now_iso())
                await db.commit()
                return count
            except Exception:
                await db.rollback()
                raise

    async def due_inspection_notice_bundles(self) -> list[list[dict[str, Any]]]:
        """보낼 차례인 노드마다 그 노드의 보내지 않은 줄 전부(한 묶음)."""
        async with self._mutation_lock:
            db = self._db
            assert db is not None
            return await inspection_notice.due_notice_bundles(db, now_iso())

    async def mark_inspection_notices_sent(self, notice_ids: list[str]) -> None:
        if not notice_ids:
            return
        async with self._mutation_lock:
            db = self._db
            assert db is not None
            try:
                await inspection_notice.mark_sent(db, notice_ids, now_iso())
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def note_inspection_notice_failures(
        self,
        notice_ids: list[str],
        *,
        error: str,
        base_sec: float,
        cap_sec: float,
    ) -> None:
        if not notice_ids:
            return
        async with self._mutation_lock:
            db = self._db
            assert db is not None
            try:
                await inspection_notice.record_failure(
                    db,
                    notice_ids,
                    error=error,
                    now=datetime.now(timezone.utc),
                    base_sec=base_sec,
                    cap_sec=cap_sec,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def inspection_notice_summaries(
        self, deploy_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        async with self._mutation_lock:
            db = self._db
            assert db is not None
            return await inspection_notice.latest_notices_by_deploy(db, deploy_ids)
