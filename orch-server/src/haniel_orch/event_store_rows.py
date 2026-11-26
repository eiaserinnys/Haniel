"""Row decoding helpers shared by EventStore modules."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from .statechart.columns import LEAF_COLUMNS, TAG_ON


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def batches(values: Sequence[str], size: int = 500) -> Iterator[Sequence[str]]:
    """IN 목록이 SQLite 변수 상한을 넘지 않게 나눈다."""
    for start in range(0, len(values), size):
        yield values[start : start + size]


def history_order(updated_at: str, created_at: str, deploy_id: str) -> str:
    """이력 한 출처의 정렬과 자르기. 합친 뒤 파이썬 정렬 (updated_at or created_at or "", deploy_id) 역순과 같은 순서다."""
    return (
        f"ORDER BY COALESCE(NULLIF({updated_at}, ''), NULLIF({created_at}, ''), '') DESC, "
        f"{deploy_id} DESC LIMIT ?"
    )


async def close_probes(
    db: aiosqlite.Connection,
    deploy_ids: Sequence[str],
    *,
    stage: str,
    reason: str,
    error: str,
    now: str | None = None,
) -> None:
    """닫힌 요청의 열린 계획 묻기를 닫는다(계획 묻기 줄은 정의 밖 저장)."""
    if not deploy_ids:
        return
    placeholders = ", ".join("?" for _ in deploy_ids)
    await db.execute(
        f"""UPDATE deploy_plan_probes
            SET status = 'terminal', terminal_kind = 'preflight_stale',
                terminal_stage = ?, terminal_reason = ?, terminal_error = ?,
                completed_at = ?
            WHERE deploy_id IN ({placeholders})
              AND status IN ('active','proposed')""",
        (stage, reason, error, now or now_iso(), *deploy_ids),
    )


def row_to_dict(cursor: aiosqlite.Cursor, row: tuple) -> dict[str, Any]:
    """Convert a row tuple to a dict using cursor.description."""
    return {
        col[0]: val
        for col, val in zip(cursor.description, row)
        if col[0] not in LEAF_COLUMNS
    }


def decode_deploy_row(cursor: aiosqlite.Cursor, row: tuple) -> dict[str, Any]:
    """Decode JSON and SQLite boolean fields from one canonical deploy row."""
    item = row_to_dict(cursor, row)
    item["commits"] = json.loads(item.pop("commits_json"))
    item["affected_services"] = json.loads(item.pop("affected_services_json"))
    item["is_self_update"] = bool(item.get("is_self_update", 0))
    # 닫힌 뒤 적용 꼬리표(잎 칸은 거른다). 그 시각과 시도 번호는 데이터 칸이라 그대로 실린다. 옛 줄은 False
    raw = {col[0]: val for col, val in zip(cursor.description, row)}
    item["applied_after_close"] = raw.get("sm_tag") == TAG_ON
    return item
