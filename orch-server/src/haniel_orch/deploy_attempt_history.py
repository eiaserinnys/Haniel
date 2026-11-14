"""이력(GET /api/orch/history)에 싣는 시도 항목과 성공 시도의 덧붙임."""

from __future__ import annotations

import json
from typing import Any


class DeployAttemptHistory:
    """Read-only history rows built from attempt and probe tables."""

    async def history_rows(self, include_superseded: bool) -> list[dict[str, Any]]:
        attempt_cursor = await self._db.execute(
            "SELECT * FROM deploy_attempts WHERE outcome = 'failed'"
        )
        attempts = self._rows(attempt_cursor, await attempt_cursor.fetchall())
        rows: list[dict[str, Any]] = []
        for attempt in attempts:
            rows.append(
                {
                    "deploy_id": f"attempt:{attempt['orchestrator_attempt_id']}",
                    "canonical_deploy_id": attempt["deploy_id"],
                    "node_id": attempt["node_id"],
                    "repo": attempt["repo"],
                    "branch": attempt["branch"],
                    "status": "success"
                    if "success" in attempt["outcome"]
                    else "failed",
                    "commits": json.loads(attempt["commits_json"]),
                    "affected_services": json.loads(attempt["affected_services_json"]),
                    "diff_stat": attempt["diff_stat"],
                    "detected_at": attempt["detected_at"],
                    "approved_by": attempt["approved_by"],
                    "error": attempt["terminal_error"] or attempt["result_error"],
                    "duration_ms": attempt["duration_ms"],
                    "created_at": attempt["started_at"],
                    "updated_at": attempt["completed_at"],
                    "attempt_outcome": attempt["outcome"],
                    "terminal_kind": attempt["terminal_kind"],
                    "terminal_stage": attempt["terminal_stage"],
                    "terminal_reason": attempt["terminal_reason"],
                    "terminal_error": attempt["terminal_error"],
                }
            )
        probe_cursor = await self._db.execute(
            "SELECT * FROM deploy_plan_probes WHERE status = 'terminal'"
        )
        for probe in self._rows(probe_cursor, await probe_cursor.fetchall()):
            rows.append(
                {
                    "deploy_id": f"preflight:{probe['probe_id']}",
                    "canonical_deploy_id": probe["deploy_id"],
                    "node_id": probe["node_id"],
                    "repo": probe["repo"],
                    "branch": probe["branch"],
                    "status": "failed",
                    "commits": [],
                    "affected_services": [],
                    "diff_stat": None,
                    "detected_at": probe["created_at"],
                    "approved_by": None,
                    "error": probe["terminal_error"],
                    "duration_ms": None,
                    "created_at": probe["created_at"],
                    "updated_at": probe["completed_at"],
                    "terminal_kind": probe["terminal_kind"],
                    "terminal_stage": probe["terminal_stage"],
                    "terminal_reason": probe["terminal_reason"],
                    "terminal_error": probe["terminal_error"],
                }
            )
        return rows

    async def success_metadata(self) -> dict[str, dict[str, Any]]:
        """Join the successful attempt onto its canonical History row once."""
        cursor = await self._db.execute(
            """SELECT * FROM deploy_attempts
               WHERE outcome IN ('success','evidence_recovered_success')
               ORDER BY completed_at DESC"""
        )
        metadata: dict[str, dict[str, Any]] = {}
        for attempt in self._rows(cursor, await cursor.fetchall()):
            metadata.setdefault(
                attempt["deploy_id"],
                {
                    "orchestrator_attempt_id": attempt["orchestrator_attempt_id"],
                    "execution_mode": attempt["execution_mode"],
                    "attempt_outcome": attempt["outcome"],
                    "journal_attempt_id": attempt["journal_attempt_id"],
                },
            )
        return metadata
