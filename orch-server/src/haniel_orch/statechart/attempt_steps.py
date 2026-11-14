"""시도 줄의 한 걸음: deploy_attempts에 쓰는 SQL이 있는 유일한 곳(스키마 초기화 제외).

요청 줄의 걸음은 `steps`에 있다. 걸음의 규약(저장소 변경 잠금, 한 트랜잭션, 전이가 없으면
`NoTransition`이고 줄에는 아무것도 쓰이지 않음)은 같다.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, NamedTuple

import aiosqlite

from .columns import ATTEMPT_REGION, attempt_leaf_is_consistent
from .definitions import ATTEMPT_TERMINAL, AttemptEvidence
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


class AttemptStep(NamedTuple):
    previous: str
    leaf: str


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
