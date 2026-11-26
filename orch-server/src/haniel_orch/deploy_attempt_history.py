"""이력(GET /api/orch/history)에 싣는 시도 항목과 성공 시도의 덧붙임."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from .event_store_rows import batches, history_order, row_to_dict


def failure_stage_fields(attempt: dict[str, Any]) -> dict[str, Any]:
    """실패한 시도의 단계와 복구 여부. 노드가 보낸 진행 알림(progress_stage, failed_stage)에서만 읽는다.

    recovery_attempted: recovering 알림을 받았으면(recovery_seen=1) true, 알림은 받았지만 recovering이 없으면 false, 알림이 없으면 null.
    failed_stage: 복구했으면 처음 들어온 recovering 직전 단계(없을 수 있다), 아니면 마지막 단계. 알 수 없으면 null.
    복구가 성공했는지는 노드의 error 문장에만 있어 허브는 읽지 않는다.
    """
    if attempt["recovery_seen"]:
        return {"failed_stage": attempt["failed_stage"], "recovery_attempted": True}
    last = attempt["progress_stage"]
    if last is None:
        return {"failed_stage": None, "recovery_attempted": None}
    return {"failed_stage": last, "recovery_attempted": False}


class DeployAttemptHistory:
    """Read-only history rows built from attempt and probe tables."""

    async def history_rows(self, limit: int) -> list[dict[str, Any]]:
        attempt_cursor = await self._db.execute(
            "SELECT * FROM deploy_attempts WHERE outcome = 'failed' "
            + history_order(
                "completed_at", "started_at", "'attempt:' || orchestrator_attempt_id"
            ),
            (limit,),
        )
        attempts = [row_to_dict(attempt_cursor, row) for row in await attempt_cursor.fetchall()]
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
                    # 오케스트레이터가 스스로 닫은 시도의 증거가 그 뒤 종결된 결과(success, failed). 그 밖은 null
                    "late_evidence": attempt["late_evidence"],
                    # 시도를 열 때 그 노드 연결의 판(wire-v1). 판 없는 시도는 null
                    "node_protocol": attempt["node_protocol"],
                    **failure_stage_fields(attempt),
                }
            )
        probe_cursor = await self._db.execute(
            "SELECT * FROM deploy_plan_probes WHERE status = 'terminal' "
            + history_order("completed_at", "created_at", "'preflight:' || probe_id"),
            (limit,),
        )
        for probe in [row_to_dict(probe_cursor, row) for row in await probe_cursor.fetchall()]:
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
                    "failed_stage": None,
                    "recovery_attempted": None,
                }
            )
        return rows

    async def success_metadata(
        self, deploy_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """Join the successful attempt onto the given canonical History rows once."""
        metadata: dict[str, dict[str, Any]] = {}
        for batch in batches(deploy_ids):
            placeholders = ", ".join("?" for _ in batch)
            cursor = await self._db.execute(
                f"""SELECT * FROM deploy_attempts
                    WHERE outcome IN ('success','evidence_recovered_success')
                      AND deploy_id IN ({placeholders})
                    ORDER BY completed_at DESC""",
                tuple(batch),
            )
            for attempt in [row_to_dict(cursor, row) for row in await cursor.fetchall()]:
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
