"""Transactional deploy-attempt state machine behind :class:`EventStore`.

노드 보고를 적는 조각은 `deploy_attempt_report_store`, 닫힌 시도의 늦은 증거는 `deploy_attempt_late_evidence`,
이력은 `deploy_attempt_history`에 있다.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from .deploy_attempt_generation_store import DeployAttemptGenerationStore
from .deploy_attempt_history import DeployAttemptHistory
from .deploy_attempt_late_evidence import DeployAttemptLateEvidence
from .deploy_attempt_progress_store import DeployAttemptProgressStore
from .deploy_attempt_report_store import DeployAttemptReportStore
from .deploy_attempt_store_support import DeployAttemptStoreSupport
from .deploy_attempt_validation import DeployAttemptValidation
from .protocol import DeployPlanProposal, DeployStatus


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expired(deadline: str) -> bool:
    return datetime.fromisoformat(deadline) <= datetime.now(timezone.utc)


class DeployAttemptStore(
    DeployAttemptGenerationStore,
    DeployAttemptProgressStore,
    DeployAttemptReportStore,
    DeployAttemptLateEvidence,
    DeployAttemptHistory,
    DeployAttemptValidation,
    DeployAttemptStoreSupport,
):
    """One-lock transaction boundary for probes, attempts, and retry markers."""

    def __init__(self, db: aiosqlite.Connection, mutation_lock: asyncio.Lock) -> None:
        self._db = db
        self._mutation_lock = mutation_lock

    async def has_retry_requirement(self, deploy_id: str) -> bool:
        cursor = await self._db.execute(
            "SELECT 1 FROM deploy_retry_requirements WHERE deploy_id = ?",
            (deploy_id,),
        )
        return await cursor.fetchone() is not None

    async def inspection_required(self, deploy_id: str) -> bool:
        """점검 필요가 켜져 있는가. 자동 요청, 탐침 만들기, 일괄 승인이 시도를 열기 전에 본다."""
        return await self._lock_on(deploy_id)

    async def get_retry_requirement(self, deploy_id: str) -> dict[str, Any] | None:
        cursor = await self._db.execute(
            "SELECT * FROM deploy_retry_requirements WHERE deploy_id = ?",
            (deploy_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        result = {column[0]: value for column, value in zip(cursor.description, row)}
        lineage_cursor = await self._db.execute(
            "SELECT lineage_orchestrator_attempt_id FROM deploy_retry_source_attempts "
            "WHERE deploy_id = ? ORDER BY added_at",
            (deploy_id,),
        )
        result["lineage"] = [item[0] for item in await lineage_cursor.fetchall()]
        return result

    async def create_probe(
        self,
        *,
        probe_id: str,
        deploy_id: str,
        connection_generation: str,
        deadline_at: str,
        manual_retry: bool,
        requested_orchestrator_attempt_id: str | None = None,
    ) -> dict[str, Any]:
        async with self._mutation_lock:
            try:
                event = await self._event(deploy_id)
                if event is None or event["status"] != DeployStatus.PENDING.value:
                    raise ValueError("canonical is not pending")
                if not await self._is_latest(event):
                    raise ValueError("canonical is stale")
                retry = await self._retry_unlocked(deploy_id)
                if manual_retry and retry is None:
                    raise ValueError("manual retry requires retry marker")
                if not manual_retry and await self._lock_on(deploy_id):
                    raise PermissionError("retry_requires_manual_approval")
                lineage = retry["lineage"] if retry else []
                await self._db.execute(
                    """INSERT INTO deploy_plan_probes
                       (probe_id, connection_generation, deploy_id, node_id, repo,
                        branch, target_head, requested_orchestrator_attempt_id,
                        source_orchestrator_attempt_id, retry_lineage_json,
                        expected_manifest_identity, expected_manifest_digest,
                        created_at, deadline_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        probe_id,
                        connection_generation,
                        deploy_id,
                        event["node_id"],
                        event["repo"],
                        event["branch"],
                        event["target_head"] or deploy_id.rsplit(":", 1)[-1],
                        requested_orchestrator_attempt_id,
                        retry and retry["source_orchestrator_attempt_id"],
                        json.dumps(lineage),
                        event["expected_manifest_identity"],
                        event["expected_manifest_digest"],
                        _now_iso(),
                        deadline_at,
                    ),
                )
                await self._db.commit()
                return await self._probe(probe_id)  # type: ignore[return-value]
            except Exception:
                await self._db.rollback()
                raise

    async def begin_normal_attempt(
        self,
        *,
        orchestrator_attempt_id: str,
        deploy_id: str,
        connection_generation: str,
        current_generation: str,
        source: str,
        approved_by: str,
        deadline_at: str,
    ) -> dict[str, Any]:
        async with self._mutation_lock:
            try:
                if connection_generation != current_generation:
                    raise ValueError("connection generation changed")
                event = await self._event(deploy_id)
                if event is None or event["status"] != DeployStatus.PENDING.value:
                    raise ValueError("canonical is not pending")
                if not await self._is_latest(event):
                    raise ValueError("canonical is stale")
                retry = await self._retry_unlocked(deploy_id)
                if retry is not None and retry["source_orchestrator_attempt_id"] is not None:
                    # 실행했을 수 있는 시도가 있으면 계획부터 묻는다. source가 빈 표시 줄(스스로 한 배포의 실패,
                    # 첫 시도의 보내기 실패)은 옛 노드 계획 함수가 시도 번호 없는 저널을 닫아 버리므로 첫 승인 길로 간다
                    raise ValueError("retry requires preflight")
                if await self._has_active_branch(event):
                    raise PermissionError("active_attempt_conflict")
                await self._insert_attempt(
                    event=event,
                    orchestrator_attempt_id=orchestrator_attempt_id,
                    connection_generation=connection_generation,
                    source=source,
                    approved_by=approved_by,
                    deadline_at=deadline_at,
                    mode="execute",
                )
                await self._db.commit()
                return await self._attempt(orchestrator_attempt_id)  # type: ignore[return-value]
            except Exception:
                await self._db.rollback()
                raise

    async def record_proposal_and_begin(
        self,
        proposal: DeployPlanProposal,
        *,
        orchestrator_attempt_id: str,
        source: str,
        approved_by: str,
        deadline_at: str,
        current_generation: str,
    ) -> dict[str, Any]:
        async with self._mutation_lock:
            try:
                probe = await self._probe(proposal.probe_id)
                if probe is None:
                    return {"status": "ignored", "reason": "unknown_probe"}
                if (
                    probe["status"] == "begun"
                    and probe["proposal_fingerprint"] == proposal.fingerprint
                ):
                    return {"status": "duplicate_begun"}
                if probe["status"] != "active":
                    return {"status": "ignored", "reason": "terminal_probe"}
                if (
                    proposal.connection_generation != probe["connection_generation"]
                    or probe["connection_generation"] != current_generation
                ):
                    await self._terminalize_probe_unlocked(
                        probe,
                        "preflight_stale",
                        "proposal",
                        "connection_generation_changed",
                        "proposal, stored probe, and current connection generation differ",
                    )
                    await self._db.commit()
                    return {"status": "terminal", "kind": "preflight_stale"}
                stale = await self._proposal_stale_reason(probe, proposal, source)
                if stale:
                    reason, error = stale
                    await self._terminalize_probe_unlocked(
                        probe, "preflight_stale", "proposal", reason, error
                    )
                    await self._db.commit()
                    return {
                        "status": "terminal",
                        "kind": "preflight_stale",
                        "reason": reason,
                    }
                await self._db.execute(
                    "UPDATE deploy_plan_probes SET status = 'proposed', mode = ?, "
                    "proposal_fingerprint = ?, proposal_json = ? WHERE probe_id = ?",
                    (
                        proposal.mode,
                        proposal.fingerprint,
                        proposal.model_dump_json(),
                        proposal.probe_id,
                    ),
                )
                if proposal.mode == "fail_closed":
                    await self._terminalize_probe_unlocked(
                        {**probe, "status": "proposed"},
                        "preflight_fail_closed",
                        "proposal",
                        proposal.reason,
                        proposal.error or proposal.reason,
                    )
                    await self._db.commit()
                    return {"status": "terminal", "kind": "preflight_fail_closed"}
                if proposal.mode == "evidence_recovery":
                    error = self._validate_recovery_proposal(probe, proposal)
                    if error:
                        await self._terminalize_probe_unlocked(
                            {**probe, "status": "proposed"},
                            "preflight_fail_closed",
                            "proposal",
                            "invalid_recovery_evidence",
                            error,
                        )
                        await self._db.commit()
                        return {"status": "terminal", "kind": "preflight_fail_closed"}
                elif proposal.mode == "execute":
                    error = self._validate_execute_proposal(probe, proposal)
                    if error:
                        await self._terminalize_probe_unlocked(
                            {**probe, "status": "proposed"},
                            "preflight_fail_closed",
                            "proposal",
                            "unsafe_execute_plan",
                            error,
                        )
                        await self._db.commit()
                        return {"status": "terminal", "kind": "preflight_fail_closed"}
                event = await self._event(probe["deploy_id"])
                if event is None:
                    raise RuntimeError("canonical disappeared")
                if await self._has_active_branch(event):
                    await self._terminalize_probe_unlocked(
                        {**probe, "status": "proposed"},
                        "preflight_stale",
                        "begin",
                        "active_attempt_conflict",
                        "another attempt owns this node, repo, and branch",
                    )
                    await self._db.commit()
                    return {"status": "terminal", "kind": "preflight_stale"}
                await self._insert_attempt(
                    event=event,
                    orchestrator_attempt_id=orchestrator_attempt_id,
                    connection_generation=probe["connection_generation"],
                    source=source,
                    approved_by=approved_by,
                    deadline_at=deadline_at,
                    mode=proposal.mode,
                    probe=probe,
                    proposal=proposal,
                )
                await self._db.execute(
                    "UPDATE deploy_plan_probes SET status = 'begun' WHERE probe_id = ?",
                    (probe["probe_id"],),
                )
                await self._db.commit()
                return {
                    "status": "begun",
                    "attempt": await self._attempt(orchestrator_attempt_id),
                }
            except Exception:
                await self._db.rollback()
                raise

    async def fail_active_attempt(
        self,
        orchestrator_attempt_id: str,
        *,
        kind: str,
        stage: str,
        reason: str,
        error: str,
    ) -> dict[str, Any]:
        """오케스트레이터가 스스로 내는 닫힘(노드 글이 아닌 닫힘). 증거 잎은 옮기지 않는다."""
        async with self._mutation_lock:
            try:
                attempt = await self._attempt(orchestrator_attempt_id)
                if attempt is None or attempt["outcome"] != "active":
                    return {"status": "ignored"}
                result = await self._fail_attempt_unlocked(
                    attempt=attempt,
                    kind=kind,
                    stage=stage,
                    reason=reason,
                    error=error,
                    self_closed=True,
                )
                await self._db.commit()
                return result
            except Exception:
                await self._db.rollback()
                raise

    async def fail_attempt_not_sent(
        self, orchestrator_attempt_id: str
    ) -> dict[str, Any]:
        """허가가 노드에 닿지 못했을 때 시도를 그 자리에서 실패로 닫는다(보내기 실패)."""
        async with self._mutation_lock:
            try:
                attempt = await self._attempt(orchestrator_attempt_id)
                if attempt is None or attempt["outcome"] != "active":
                    return {"status": "ignored"}
                result = await self._fail_attempt_unlocked(
                    attempt=attempt,
                    kind="delivery_failed",
                    stage="approval_send",
                    reason="approval_send_failed",
                    error="approval could not be delivered to the node",
                    not_sent=True,
                )
                await self._db.commit()
                return result
            except Exception:
                await self._db.rollback()
                raise

    async def get_attempt(self, orchestrator_attempt_id: str) -> dict[str, Any] | None:
        return await self._attempt(orchestrator_attempt_id)

    async def terminalize_preflight(
        self, probe_id: str, *, kind: str, stage: str, reason: str, error: str
    ) -> bool:
        async with self._mutation_lock:
            try:
                probe = await self._probe(probe_id)
                if probe is None:
                    return False
                changed = await self._terminalize_probe_unlocked(
                    probe, kind, stage, reason, error
                )
                await self._db.commit()
                return changed
            except Exception:
                await self._db.rollback()
                raise

    async def get_active_attempts(self) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            "SELECT * FROM deploy_attempts WHERE outcome = 'active'"
        )
        return self._rows(cursor, await cursor.fetchall())

    async def get_active_probes(self) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            "SELECT * FROM deploy_plan_probes WHERE status IN ('active','proposed')"
        )
        return self._rows(cursor, await cursor.fetchall())

    async def get_probe(self, probe_id: str) -> dict[str, Any] | None:
        return await self._probe(probe_id)
