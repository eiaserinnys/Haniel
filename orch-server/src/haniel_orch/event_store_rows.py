"""Row decoding helpers shared by EventStore modules."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_to_dict(cursor: aiosqlite.Cursor, row: tuple) -> dict[str, Any]:
    """Convert a row tuple to a dict using cursor.description."""
    return {col[0]: val for col, val in zip(cursor.description, row)}


def decode_deploy_row(cursor: aiosqlite.Cursor, row: tuple) -> dict[str, Any]:
    """Decode JSON and SQLite boolean fields from one canonical deploy row."""
    item = row_to_dict(cursor, row)
    item["commits"] = json.loads(item.pop("commits_json"))
    item["affected_services"] = json.loads(item.pop("affected_services_json"))
    item["is_self_update"] = bool(item.get("is_self_update", 0))
    return item
