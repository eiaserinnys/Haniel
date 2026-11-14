"""Durable reconciliation for deployments initiated outside orchestrator attempts."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from .event_store_rows import now_iso
from .protocol import DeployStatus, NodeDeployReport
from .statechart import attempt_steps, steps
from .statechart.columns import LEAF_COLUMNS
from .statechart.restore import NoTransition


def _rows(
    cursor: aiosqlite.Cursor, rows: list[tuple[Any, ...]]
) -> list[dict[str, Any]]:
    columns = [column[0] for column in cursor.description or ()]
    return [
        {k: v for k, v in zip(columns, row) if k not in LEAF_COLUMNS} for row in rows
    ]


async def _attempt(
    db: aiosqlite.Connection, node_attempt_id: str
) -> dict[str, Any] | None:
    cursor = await db.execute(
        "SELECT * FROM node_deploy_attempts WHERE node_attempt_id = ?",
        (node_attempt_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return _rows(cursor, [row])[0]


async def _canonical(
    db: aiosqlite.Connection, report: NodeDeployReport
) -> dict[str, Any] | None:
    cursor = await db.execute(
        """SELECT * FROM deploy_events
           WHERE deploy_id = ? AND node_id = ? AND repo = ? AND branch = ?
             AND target_head = ?""",
        (
            report.deploy_id,
            report.node_id,
            report.repo,
            report.branch,
            report.target_head,
        ),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return _rows(cursor, [row])[0]


def _validate_identity(existing: dict[str, Any], report: NodeDeployReport) -> None:
    expected = {
        "journal_attempt_id": report.journal_attempt_id,
        "deploy_id": report.deploy_id,
        "node_id": report.node_id,
        "repo": report.repo,
        "branch": report.branch,
        "target_head": report.target_head,
    }
    conflicts = [
        f"{key} stored={existing.get(key)!r} provided={value!r}"
        for key, value in expected.items()
        if existing.get(key) != value
    ]
    if conflicts:
        raise ValueError(
            "node deploy attempt identity changed: " + ", ".join(conflicts)
        )


async def _insert_started(
    db: aiosqlite.Connection, report: NodeDeployReport
) -> dict[str, Any]:
    await db.execute(
        """INSERT INTO node_deploy_attempts
           (node_attempt_id, journal_attempt_id, deploy_id, node_id, repo, branch,
            target_head, trigger, started_local_head, started_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            report.node_attempt_id,
            report.journal_attempt_id,
            report.deploy_id,
            report.node_id,
            report.repo,
            report.branch,
            report.target_head,
            report.trigger,
            report.local_head,
            now_iso(),
        ),
    )
    return await _attempt(db, report.node_attempt_id)  # type: ignore[return-value]


async def _record_prior_failure(db: aiosqlite.Connection, report: NodeDeployReport) -> bool:
    """요청 줄이 아직 없을 때 온 스스로 한 배포의 실패(G12, wire-v1 6절)를 노드 시도 줄에 끝난 채로 적는다.

    같은 deploy_id의 요청 줄이 뒤에 생기면 `steps.create_request`가 이 줄을 보고 점검 필요를 켠 채 시작한다.
    이력과 대기 목록의 직전 실패에 그 까닭이 보인다. 이미 적은 노드 시도면 False다.
    """
    if await _attempt(db, report.node_attempt_id) is not None:
        return False
    now = now_iso()
    await db.execute(
        """INSERT INTO node_deploy_attempts
           (node_attempt_id, journal_attempt_id, deploy_id, node_id, repo, branch,
            target_head, trigger, outcome, started_local_head, terminal_local_head, error,
            duration_ms, started_at, completed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'failed', ?, ?, ?, ?, ?, ?)""",
        (
            report.node_attempt_id,
            report.journal_attempt_id,
            report.deploy_id,
            report.node_id,
            report.repo,
            report.branch,
            report.target_head,
            report.trigger,
            report.local_head,
            report.local_head,
            report.error,
            report.duration_ms,
            now,
            now,
        ),
    )
    return True


async def record(db: aiosqlite.Connection, report: NodeDeployReport) -> dict[str, Any]:
    """Apply one report inside the caller-owned SQLite transaction.

    closed_now는 이 글이 노드 시도나 요청을 닫았는가다(판 1 연결에 보내는 「종결을 받았다」의 outcome).
    """
    canonical = await _canonical(db, report)
    if canonical is None or canonical["status"] not in {
        DeployStatus.PENDING.value,
        DeployStatus.DEPLOYING.value,
    }:
        closed_now = False
        if canonical is None and report.phase == "failed":
            closed_now = await _record_prior_failure(db, report)
        if (
            canonical is not None
            and canonical["status"] == DeployStatus.REJECTED.value
            and report.phase == "succeeded"
        ):
            # 거절이나 밀림으로 닫힌 요청에 닿은 성공: 「닫힌 뒤 적용」 꼬리표만 남긴다(어긋남 12). 답은 지금 그대로다.
            # 노드 시도 번호에는 순서가 없으므로 「뒤 시도 없음」은 보지 않는다(거짓으로 둔다)
            try:
                await steps.step_request(
                    db,
                    report.deploy_id,
                    "success_evidence",
                    facts={"no_later": False},
                    data={"attempt_id": report.node_attempt_id},
                    now=now_iso(),
                )
            except NoTransition as exc:
                exc.warn("node_deploy_report")
        return {
            "status": "ignored",
            "deploy_id": report.deploy_id,
            "cancelled_orchestrator_attempt_ids": [],
            "closed_now": closed_now,
        }

    existing = await _attempt(db, report.node_attempt_id)
    if existing is None:
        existing = await _insert_started(db, report)
    else:
        _validate_identity(existing, report)
        if existing["outcome"] != "active":
            return {
                "status": "ignored",
                "deploy_id": report.deploy_id,
                "cancelled_orchestrator_attempt_ids": [],
                "closed_now": False,
            }

    now = now_iso()
    if report.phase == "started":
        return {
            "status": "started",
            "canonical_status": canonical["status"],
            "deploy_id": report.deploy_id,
            "cancelled_orchestrator_attempt_ids": [],
            "closed_now": False,
        }

    outcome = "success" if report.phase == "succeeded" else "failed"
    await db.execute(
        """UPDATE node_deploy_attempts
           SET outcome = ?, terminal_local_head = ?, error = ?, duration_ms = ?,
               completed_at = ?
           WHERE node_attempt_id = ? AND outcome = 'active'""",
        (
            outcome,
            report.local_head,
            report.error,
            report.duration_ms,
            now,
            report.node_attempt_id,
        ),
    )

    active_cursor = await db.execute(
        """SELECT orchestrator_attempt_id FROM deploy_attempts
           WHERE deploy_id = ? AND outcome = 'active'
           ORDER BY orchestrator_attempt_id""",
        (report.deploy_id,),
    )
    active_orchestrator_ids = [row[0] for row in await active_cursor.fetchall()]

    if report.phase == "succeeded":
        for attempt_id in active_orchestrator_ids:
            await attempt_steps.step_attempt(
                db, attempt_id, "superseded_by_node", now=now
            )
        await steps.step_request(
            db,
            report.deploy_id,
            "node_succeeded",
            data={"duration_ms": report.duration_ms},
            now=now,
        )
        await db.execute(
            """UPDATE deploy_plan_probes
               SET status = 'terminal', terminal_kind = 'preflight_stale',
                   terminal_stage = 'node_deploy_report',
                   terminal_reason = 'canonical_terminal',
                   terminal_error = 'canonical succeeded out of band',
                   completed_at = ?
               WHERE deploy_id = ? AND status IN ('active','proposed')""",
            (now, report.deploy_id),
        )
        canonical_status = DeployStatus.SUCCESS.value
    else:
        # 노드가 스스로 한 배포의 실패는 점검 필요를 켠다. 도는 시도가 있으면 칸만 켜고 시도와 요청 상태는 그대로 둔다
        try:
            await steps.step_request(
                db,
                report.deploy_id,
                "node_failed",
                facts={"no_active_attempt": not active_orchestrator_ids},
                now=now,
            )
        except NoTransition as exc:
            # 점검 필요가 이미 켜져 있고 요청 상태도 옮길 곳이 없을 때
            exc.warn("node_failed")
        canonical_status = (
            DeployStatus.DEPLOYING.value
            if active_orchestrator_ids
            else DeployStatus.PENDING.value
        )

    return {
        "status": outcome,
        "canonical_status": canonical_status,
        "deploy_id": report.deploy_id,
        "cancelled_orchestrator_attempt_ids": (
            active_orchestrator_ids if report.phase == "succeeded" else []
        ),
        "closed_now": True,
    }


async def history_rows(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    cursor = await db.execute(
        "SELECT * FROM node_deploy_attempts WHERE outcome = 'failed'"
    )
    attempts = _rows(cursor, await cursor.fetchall())
    rows: list[dict[str, Any]] = []
    for attempt in attempts:
        canonical_cursor = await db.execute(
            "SELECT commits_json, affected_services_json, diff_stat, detected_at "
            "FROM deploy_events WHERE deploy_id = ?",
            (attempt["deploy_id"],),
        )
        canonical = await canonical_cursor.fetchone()
        commits = json.loads(canonical[0]) if canonical else []
        services = json.loads(canonical[1]) if canonical else []
        rows.append(
            {
                "deploy_id": f"node-attempt:{attempt['node_attempt_id']}",
                "canonical_deploy_id": attempt["deploy_id"],
                "node_id": attempt["node_id"],
                "repo": attempt["repo"],
                "branch": attempt["branch"],
                "status": "failed",
                "commits": commits,
                "affected_services": services,
                "diff_stat": canonical[2] if canonical else None,
                "detected_at": canonical[3] if canonical else attempt["started_at"],
                "approved_by": None,
                "error": attempt["error"],
                "duration_ms": attempt["duration_ms"],
                "created_at": attempt["started_at"],
                "updated_at": attempt["completed_at"],
                "attempt_outcome": "failed",
                "terminal_kind": "node_deploy_failed",
                "terminal_stage": attempt["trigger"],
                "terminal_reason": "out_of_band_failure",
                "terminal_error": attempt["error"],
                "node_attempt_id": attempt["node_attempt_id"],
                "journal_attempt_id": attempt["journal_attempt_id"],
            }
        )
    return rows


async def success_metadata(db: aiosqlite.Connection) -> dict[str, dict[str, Any]]:
    cursor = await db.execute(
        """SELECT * FROM node_deploy_attempts WHERE outcome = 'success'
           ORDER BY completed_at DESC"""
    )
    metadata: dict[str, dict[str, Any]] = {}
    for attempt in _rows(cursor, await cursor.fetchall()):
        metadata.setdefault(
            attempt["deploy_id"],
            {
                "node_attempt_id": attempt["node_attempt_id"],
                "node_deploy_trigger": attempt["trigger"],
                "attempt_outcome": "success",
                "journal_attempt_id": attempt["journal_attempt_id"],
            },
        )
    return metadata
