"""Durable reconciliation for deployments initiated outside orchestrator attempts."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from .event_store_rows import now_iso
from .protocol import DeployStatus, NodeDeployReport


def _rows(
    cursor: aiosqlite.Cursor, rows: list[tuple[Any, ...]]
) -> list[dict[str, Any]]:
    columns = [column[0] for column in cursor.description or ()]
    return [dict(zip(columns, row)) for row in rows]


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


async def record(db: aiosqlite.Connection, report: NodeDeployReport) -> dict[str, Any]:
    """Apply one report inside the caller-owned SQLite transaction."""
    canonical = await _canonical(db, report)
    if canonical is None or canonical["status"] not in {
        DeployStatus.PENDING.value,
        DeployStatus.DEPLOYING.value,
    }:
        return {
            "status": "ignored",
            "deploy_id": report.deploy_id,
            "cancelled_orchestrator_attempt_ids": [],
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
            }

    now = now_iso()
    if report.phase == "started":
        return {
            "status": "started",
            "canonical_status": canonical["status"],
            "deploy_id": report.deploy_id,
            "cancelled_orchestrator_attempt_ids": [],
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
        await db.execute(
            """UPDATE deploy_attempts
               SET outcome = 'superseded', terminal_kind = 'node_deploy_succeeded',
                   terminal_stage = 'node_deploy_report',
                   terminal_reason = 'out_of_band_success',
                   terminal_error = 'superseded by node-owned deployment',
                   completed_at = ?
               WHERE deploy_id = ? AND outcome = 'active'""",
            (now, report.deploy_id),
        )
        await db.execute(
            """UPDATE deploy_events
               SET status = ?, approved_by = NULL, error = NULL, duration_ms = ?,
                   updated_at = ?
               WHERE deploy_id = ? AND status IN (?, ?)""",
            (
                DeployStatus.SUCCESS.value,
                report.duration_ms,
                now,
                report.deploy_id,
                DeployStatus.PENDING.value,
                DeployStatus.DEPLOYING.value,
            ),
        )
        await db.execute(
            "DELETE FROM deploy_retry_requirements WHERE deploy_id = ?",
            (report.deploy_id,),
        )
        await db.execute(
            "DELETE FROM deploy_retry_source_attempts WHERE deploy_id = ?",
            (report.deploy_id,),
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
    elif active_orchestrator_ids:
        canonical_status = DeployStatus.DEPLOYING.value
    else:
        await db.execute(
            """UPDATE deploy_events
               SET status = ?, approved_by = NULL, error = NULL, duration_ms = NULL,
                   updated_at = ?
               WHERE deploy_id = ? AND status = ?""",
            (
                DeployStatus.PENDING.value,
                now,
                report.deploy_id,
                DeployStatus.DEPLOYING.value,
            ),
        )
        canonical_status = DeployStatus.PENDING.value

    return {
        "status": outcome,
        "canonical_status": canonical_status,
        "deploy_id": report.deploy_id,
        "cancelled_orchestrator_attempt_ids": (
            active_orchestrator_ids if report.phase == "succeeded" else []
        ),
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
