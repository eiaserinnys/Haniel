"""Internal SQL and transition support for durable deploy attempts.

이력 항목은 `deploy_attempt_history`, 노드 보고 적기는 `deploy_attempt_report_store`, 계획 답과 증거 회수의 재확인은
`deploy_attempt_validation`에 있다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiosqlite

from .protocol import DeployPlanProposal
from .statechart import attempt_steps, steps
from .statechart.columns import LEAF_COLUMNS, LOCK_ON
from .statechart.restore import REASON_NO_TRANSITION, NoTransition


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeployAttemptStoreSupport:
    """Private helpers mixed into the transaction-owning store."""

    async def _insert_attempt(
        self,
        *,
        event: dict[str, Any],
        orchestrator_attempt_id: str,
        connection_generation: str,
        source: str,
        approved_by: str,
        deadline_at: str,
        mode: str,
        probe: dict[str, Any] | None = None,
        proposal: DeployPlanProposal | None = None,
    ) -> None:
        now = _now_iso()
        # 시도 순서 칸: 같은 요청의 최댓값 더하기 1(첫 시도는 1). 자동 길처럼 노드가 시도 번호를 정해도 순서는 여기서 적는다
        cursor = await self._db.execute(
            "SELECT COALESCE(MAX(attempt_seq), 0) + 1 FROM deploy_attempts WHERE deploy_id = ?",
            (event["deploy_id"],),
        )
        attempt_seq = (await cursor.fetchone())[0]
        await attempt_steps.create_attempt(
            self._db,
            orchestrator_attempt_id=orchestrator_attempt_id,
            deploy_id=event["deploy_id"],
            node_id=event["node_id"],
            repo=event["repo"],
            branch=event["branch"],
            target_head=event["target_head"] or event["deploy_id"].rsplit(":", 1)[-1],
            source=source,
            execution_mode=mode,
            approved_by=approved_by,
            probe_id=probe and probe["probe_id"],
            connection_generation=connection_generation,
            proposal_fingerprint=proposal and proposal.fingerprint,
            source_orchestrator_attempt_id=probe
            and probe["source_orchestrator_attempt_id"],
            journal_attempt_id=proposal and proposal.journal_attempt_id,
            manifest_identity=proposal and proposal.manifest_identity,
            manifest_digest=proposal and proposal.manifest_digest,
            journal_target_head=proposal and proposal.journal_target_head,
            journal_completed_at=proposal and proposal.journal_completed_at,
            original_previous_head=proposal and proposal.original_previous_head,
            commits_json=event["commits_json"],
            affected_services_json=event["affected_services_json"],
            diff_stat=event["diff_stat"],
            detected_at=event["detected_at"],
            started_at=now,
            deadline_at=deadline_at,
            attempt_seq=attempt_seq,
        )
        try:
            await steps.step_request(
                self._db,
                event["deploy_id"],
                "begin_attempt",
                # 점검 필요를 내릴 수 있는 원천은 단건 승인 하나다(일괄 승인과 자동 요청은 못 내린다)
                facts={"manual_single": source == "manual_single"},
                data={"approved_by": approved_by},
                now=now,
            )
        except NoTransition as exc:
            if exc.reason == REASON_NO_TRANSITION and await self._lock_on(
                event["deploy_id"]
            ):
                raise PermissionError("inspection_required") from exc
            raise RuntimeError("canonical owner changed during begin") from exc

    async def _close_request_after_success(
        self, attempt: dict[str, Any]
    ) -> dict[str, Any]:
        """시도가 성공으로 끝난 뒤 요청 줄과 계획 묻기를 닫는다(같은 트랜잭션)."""
        closed = await self._attempt(attempt["orchestrator_attempt_id"])
        try:
            await steps.step_request(
                self._db,
                attempt["deploy_id"],
                "attempt_succeeded",
                data={"duration_ms": closed["duration_ms"]},
                now=closed["completed_at"],
            )
        except NoTransition as exc:
            # 요청이 배포 중이 아니면(배포 중에 거절된 뒤 시도가 성공하는 경쟁) 지금도 요청 줄 갱신은
            # 상태 조건에 걸려 0건이다. 거절이 이미 표시 줄과 계보 줄을 지웠으므로 그 뒤 지울 것도 없다
            exc.warn("attempt_succeeded")
        await self._terminalize_sibling_probes_unlocked(
            attempt["deploy_id"],
            "canonical succeeded before this probe began",
        )
        return {"status": "success", "deploy_id": attempt["deploy_id"]}

    async def _fail_attempt_unlocked(
        self,
        attempt: dict[str, Any],
        *,
        kind: str,
        stage: str,
        reason: str,
        error: str,
        extra: dict[str, Any] | None = None,
        not_sent: bool = False,
        self_closed: bool = False,
    ) -> dict[str, Any]:
        """시도를 실패로 닫고 요청을 옮긴다.

        not_sent는 허가가 노드에 닿지 못한 보내기 실패다. self_closed는 오케스트레이터가 스스로 내는 닫힘(실행 임대
        마감, 보내기 실패)으로, 증거 잎은 옮기지 않는다(뒤에 오는 늦은 글이 증거로 적힌다). 노드 글로 닫히는 것은
        닫힘과 증거를 한 걸음에 한다.
        """
        now = _now_iso()
        if self_closed or not_sent:
            closed = await attempt_steps.close_attempt(
                self._db,
                attempt["orchestrator_attempt_id"],
                kind=kind,
                stage=stage,
                reason=reason,
                error=error,
                now=now,
            )
            if not closed:
                return {"status": "ignored"}
        else:
            try:
                await attempt_steps.step_attempt(
                    self._db,
                    attempt["orchestrator_attempt_id"],
                    "fail",
                    data={
                        "kind": kind,
                        "stage": stage,
                        "reason": reason,
                        "error": error,
                        "extra": extra,
                    },
                    now=now,
                )
            except NoTransition as exc:
                exc.warn("fail")
                return {"status": "ignored"}
        event = await self._event(attempt["deploy_id"])
        if event is None:
            return {"status": "failed", "deploy_id": attempt["deploy_id"]}
        newer = not await self._is_latest(event)
        current_retry = await self._retry_unlocked(attempt["deploy_id"])
        if not_sent:
            # 실행하지 않았음이 확실한 닫힘은 계보에는 더하되 source를 옮기지 않는다(design.md:232)
            stable_source = (
                current_retry["source_orchestrator_attempt_id"] if current_retry else None
            )
        elif attempt["execution_mode"] == "evidence_recovery" and current_retry:
            stable_source = current_retry["source_orchestrator_attempt_id"]
        else:
            stable_source = attempt["orchestrator_attempt_id"]
        try:
            await steps.step_request(
                self._db,
                attempt["deploy_id"],
                "send_failed" if not_sent else "attempt_failed",
                facts={"newer": newer},
                data={
                    "failed_attempt_id": attempt["orchestrator_attempt_id"],
                    "stable_source": stable_source,
                    "marker_created_at": current_retry["created_at"]
                    if current_retry
                    else None,
                },
                now=now,
            )
        except NoTransition as exc:
            # 요청 줄이 없거나 정의에 잎이 없는 옛 값(approved, failed)이거나, 닫힌 요청에 점검 필요가
            # 이미 켜져 있을 때 닿는다
            exc.warn("send_failed" if not_sent else "attempt_failed")
        if newer:
            await self._terminalize_sibling_probes_unlocked(
                attempt["deploy_id"],
                "canonical was superseded before this probe began",
            )
            return {"status": "superseded", "deploy_id": attempt["deploy_id"]}
        await self._terminalize_sibling_probes_unlocked(
            attempt["deploy_id"],
            "attempt failed; a new manual retry probe is required",
        )
        return {"status": "failed", "deploy_id": attempt["deploy_id"]}

    async def _terminalize_sibling_probes_unlocked(
        self, deploy_id: str, error: str
    ) -> None:
        await self._db.execute(
            """UPDATE deploy_plan_probes
               SET status = 'terminal', terminal_kind = 'preflight_stale',
                   terminal_stage = 'attempt_terminal',
                   terminal_reason = 'attempt_lineage_changed',
                   terminal_error = ?, completed_at = ?
               WHERE deploy_id = ? AND status IN ('active','proposed')""",
            (error, _now_iso(), deploy_id),
        )

    async def _lock_on(self, deploy_id: str) -> bool:
        """점검 필요 잎(deploy_events.sm_lock)이 켜져 있는가."""
        cursor = await self._db.execute(
            "SELECT sm_lock FROM deploy_events WHERE deploy_id = ?", (deploy_id,)
        )
        row = await cursor.fetchone()
        return row is not None and row[0] == LOCK_ON

    async def _terminalize_probe_unlocked(
        self, probe: dict[str, Any], kind: str, stage: str, reason: str, error: str
    ) -> bool:
        changed = await self._db.execute(
            "UPDATE deploy_plan_probes SET status = 'terminal', terminal_kind = ?, "
            "terminal_stage = ?, terminal_reason = ?, terminal_error = ?, completed_at = ? "
            "WHERE probe_id = ? AND status IN ('active','proposed')",
            (kind, stage, reason, error, _now_iso(), probe["probe_id"]),
        )
        return changed.rowcount == 1

    async def _retry_unlocked(self, deploy_id: str) -> dict[str, Any] | None:
        cursor = await self._db.execute(
            "SELECT * FROM deploy_retry_requirements WHERE deploy_id = ?", (deploy_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        result = self._rows(cursor, [row])[0]
        lineage_cursor = await self._db.execute(
            "SELECT lineage_orchestrator_attempt_id FROM deploy_retry_source_attempts "
            "WHERE deploy_id = ? ORDER BY added_at",
            (deploy_id,),
        )
        result["lineage"] = [value[0] for value in await lineage_cursor.fetchall()]
        return result

    async def _event(self, deploy_id: str) -> dict[str, Any] | None:
        cursor = await self._db.execute(
            "SELECT * FROM deploy_events WHERE deploy_id = ?", (deploy_id,)
        )
        row = await cursor.fetchone()
        return None if row is None else self._rows(cursor, [row])[0]

    async def _probe(self, probe_id: str) -> dict[str, Any] | None:
        cursor = await self._db.execute(
            "SELECT * FROM deploy_plan_probes WHERE probe_id = ?", (probe_id,)
        )
        row = await cursor.fetchone()
        return None if row is None else self._rows(cursor, [row])[0]

    async def _attempt(self, orchestrator_attempt_id: str) -> dict[str, Any] | None:
        cursor = await self._db.execute(
            "SELECT * FROM deploy_attempts WHERE orchestrator_attempt_id = ?",
            (orchestrator_attempt_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else self._rows(cursor, [row])[0]

    async def _owned_attempt(
        self, deploy_id: str, orchestrator_attempt_id: str, generation: str
    ) -> dict[str, Any] | None:
        """글이 가리키는 시도 줄. 요청 번호나 허가의 세대가 다르면 모순되는 글이라 None이다(닫힌 시도도 돌려준다)."""
        attempt = await self._attempt(orchestrator_attempt_id)
        if (
            attempt is None
            or attempt["deploy_id"] != deploy_id
            or attempt["connection_generation"] != generation
        ):
            return None
        return attempt

    async def _current_attempt(
        self, deploy_id: str, orchestrator_attempt_id: str, generation: str
    ) -> dict[str, Any] | None:
        attempt = await self._owned_attempt(deploy_id, orchestrator_attempt_id, generation)
        if attempt is None or attempt["outcome"] != "active":
            return None
        return attempt

    async def _is_latest(self, event: dict[str, Any] | None) -> bool:
        if event is None:
            return False
        cursor = await self._db.execute(
            "SELECT deploy_id FROM deploy_events WHERE node_id = ? AND repo = ? "
            "AND branch = ? AND deploy_id NOT LIKE 'attempt:%' "
            "ORDER BY detected_at DESC, created_at DESC, deploy_id DESC LIMIT 1",
            (event["node_id"], event["repo"], event["branch"]),
        )
        row = await cursor.fetchone()
        return row is not None and row[0] == event["deploy_id"]

    async def _has_active_branch(self, event: dict[str, Any]) -> bool:
        cursor = await self._db.execute(
            "SELECT 1 FROM deploy_attempts WHERE node_id = ? AND repo = ? "
            "AND branch = ? AND outcome = 'active' LIMIT 1",
            (event["node_id"], event["repo"], event["branch"]),
        )
        return await cursor.fetchone() is not None

    @staticmethod
    def _rows(cursor: aiosqlite.Cursor, rows: list[tuple]) -> list[dict[str, Any]]:
        return [
            {
                column[0]: value
                for column, value in zip(cursor.description, row)
                if column[0] not in LEAF_COLUMNS
            }
            for row in rows
        ]
