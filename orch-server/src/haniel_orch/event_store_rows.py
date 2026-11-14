"""Row decoding helpers shared by EventStore modules."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from .statechart.columns import LEAF_COLUMNS, TAG_ON


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
