"""시도 줄의 한 걸음: deploy_attempts에 쓰는 SQL이 있는 유일한 곳(스키마 초기화 제외).

요청 줄의 걸음은 `steps`에 있다. 걸음의 규약(저장소 변경 잠금, 한 트랜잭션, 전이가 없으면
`NoTransition`이고 줄에는 아무것도 쓰이지 않음)은 같다.

시도의 닫힘(outcome, terminal 네 칸, closure, completed_at)과 증거 종결(sm_evidence 잎)은 따로 움직인다(design.md:79).
- 노드 글로 닫히는 것(실패 결과, deploy_attempt_terminal 등)은 `step_attempt`의 fail 사건이 닫힘과 증거를 한 걸음에 쓴다.
- 오케스트레이터가 스스로 내는 닫힘(마감, 보내기 실패, hello 목록에 없음)은 `close_attempt`가 닫힘만 쓰고 증거 잎은 두지 않는다.
- 닫힌 시도에 닿은 늦은 글은 `step_attempt`가 증거 잎과 증거 칸만 옮기고 닫힘은 바꾸지 않는다(design.md:118).
- 이미 종결된 증거에 같은 글이 또 오면(dup) 아무 칸도 쓰지 않고 기호만 돌려준다(design.md:81).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, NamedTuple

import aiosqlite

from .columns import ATTEMPT_REGION, LATE_EVIDENCE_OF_LEAF, attempt_leaf_is_consistent
from .definitions import ATTEMPT_TERMINAL, AttemptEvidence
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


class AttemptStep(NamedTuple):
    previous: str
    leaf: str
    todo: tuple[str, ...] = ()


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
    "deadline_kind",
    "attempt_seq",
    "node_protocol",
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
    event: str, leaf: str, data: Mapping[str, Any], now: str, closed: bool
) -> dict[str, Any]:
    """사건과 결과 잎에서 시도 줄에 쓸 칸을 정한다. closed면 닫힘 칸(outcome, terminal, closure, completed_at)은 쓰지 않는다."""
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
        if not closed:
            columns.update(
                outcome="failed",
                terminal_kind=data["kind"],
                terminal_stage=data["stage"],
                terminal_reason=data["reason"],
                terminal_error=data["error"],
                closure=data.get("closure", "failed"),
                completed_at=now,
            )
        extra = dict(data.get("extra") or {})
        unknown = set(extra) - _FAIL_EXTRA_COLUMNS
        if unknown:
            raise KeyError(f"fail 사건이 쓸 수 없는 칸: {sorted(unknown)}")
        columns.update(extra)
    elif event == "recovered":
        if not closed:
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
    if leaf == "ev_both" and event in ("result_ok", "settled_ok") and not closed:
        columns.update(outcome="success", completed_at=now)
    if closed and leaf in LATE_EVIDENCE_OF_LEAF:
        # 스스로 닫은 시도의 증거가 뒤늦게 종결됐다. 닫힘은 그대로고 이력에 늦은 증거로 보인다
        columns["late_evidence"] = LATE_EVIDENCE_OF_LEAF[leaf]
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
    """시도 줄에 사건 하나를 넣는다. 칸 없는 사건은 NoTransition.

    닫힌 시도(outcome이 active가 아님)에도 증거 잎이 열려 있으면 늦은 글을 증거로 적는다. 그때 닫힘 칸은 바꾸지 않는다.
    이미 종결된 증거에 같은 글이 또 오면(dup) 아무 칸도 쓰지 않고 기호 dup만 돌려준다.
    """
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
        after, todo = advance_with_todo(AttemptEvidence, {ATTEMPT_REGION: leaf}, event, given)
    except NoTransition as exc:
        exc.key = orchestrator_attempt_id
        raise
    new_leaf = after[ATTEMPT_REGION]
    if new_leaf == leaf and leaf in ATTEMPT_TERMINAL:
        return AttemptStep(previous=leaf, leaf=new_leaf, todo=tuple(todo))
    columns = {
        "sm_evidence": new_leaf,
        **_attempt_columns(event, new_leaf, data, now, closed=outcome != "active"),
    }
    assignments = ", ".join(f"{name} = ?" for name in columns)
    updated = await db.execute(
        f"UPDATE deploy_attempts SET {assignments} "
        "WHERE orchestrator_attempt_id = ? AND outcome = ? AND sm_evidence = ?",
        (*columns.values(), orchestrator_attempt_id, outcome, leaf),
    )
    if updated.rowcount != 1:
        raise NoTransition(
            AttemptEvidence.__name__,
            {"outcome": outcome, "sm_evidence": leaf},
            event,
            REASON_UPDATE_ZERO,
            orchestrator_attempt_id,
        )
    return AttemptStep(previous=leaf, leaf=new_leaf, todo=tuple(todo))


def attempt_is_terminal(leaf: str) -> bool:
    return leaf in ATTEMPT_TERMINAL


async def close_attempt(
    db: aiosqlite.Connection,
    orchestrator_attempt_id: str,
    *,
    kind: str,
    stage: str,
    reason: str,
    error: str,
    closure: str,
    now: str,
) -> bool:
    """오케스트레이터가 스스로 내는 닫힘(마감, 보내기 실패, hello 목록에 없음). 증거 잎은 옮기지 않는다.

    닫힘은 증거 정의의 상태가 아니라 시도 줄의 닫힘 칸이다. closure는 닫힘 일곱의 종류(not_executed,
    execution_unknown, failed)다. 도는 시도에만 쓰이고, 이미 닫힌 시도면 False다.
    """
    updated = await db.execute(
        "UPDATE deploy_attempts SET outcome = 'failed', terminal_kind = ?, terminal_stage = ?, "
        "terminal_reason = ?, terminal_error = ?, closure = ?, completed_at = ? "
        "WHERE orchestrator_attempt_id = ? AND outcome = 'active'",
        (kind, stage, reason, error, closure, now, orchestrator_attempt_id),
    )
    return updated.rowcount == 1


async def mark_execution_started(
    db: aiosqlite.Connection, orchestrator_attempt_id: str, now: str
) -> None:
    """실제 실행이 시작됐다는 첫 증거(진행 알림, 결과, 실행 방식의 정착)의 시각을 처음 한 번만 적는다. 닫힌 시도에도 사실만 적는다."""
    await db.execute(
        "UPDATE deploy_attempts SET execution_started_at = ? "
        "WHERE orchestrator_attempt_id = ? AND execution_started_at IS NULL",
        (now, orchestrator_attempt_id),
    )


async def renew_deadline(
    db: aiosqlite.Connection,
    orchestrator_attempt_id: str,
    deadline_at: str,
    expected_budget_sec: int | None,
) -> None:
    """실행 임대 늘림은 상태 칸이 아니라 데이터 칸이다. 마감 종류는 실행 임대가 된다(시작했다의 마감)."""
    await db.execute(
        "UPDATE deploy_attempts SET deadline_at = ?, expected_budget_sec = ?, deadline_kind = 'lease' "
        "WHERE orchestrator_attempt_id = ? AND outcome = 'active'",
        (deadline_at, expected_budget_sec, orchestrator_attempt_id),
    )


async def set_deadline(
    db: aiosqlite.Connection,
    orchestrator_attempt_id: str,
    *,
    kind: str,
    deadline_at: str | None = None,
) -> None:
    """믿음별 마감의 종류(와 시각)를 적는다(wire-v1 5절). none은 세지 않음이고 deadline_at은 마지막 값 그대로 둔다."""
    if deadline_at is None:
        await db.execute(
            "UPDATE deploy_attempts SET deadline_kind = ? "
            "WHERE orchestrator_attempt_id = ? AND outcome = 'active'",
            (kind, orchestrator_attempt_id),
        )
        return
    await db.execute(
        "UPDATE deploy_attempts SET deadline_kind = ?, deadline_at = ? "
        "WHERE orchestrator_attempt_id = ? AND outcome = 'active'",
        (kind, deadline_at, orchestrator_attempt_id),
    )
