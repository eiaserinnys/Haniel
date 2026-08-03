"""Transactional deploy-attempt state machine behind :class:`EventStore`."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from .deploy_attempt_generation_store import DeployAttemptGenerationStore
from .deploy_attempt_progress_store import DeployAttemptProgressStore
from .deploy_attempt_store_support import DeployAttemptStoreSupport
from .protocol import (
    DeployAttemptTerminal,
    DeployPlanProposal,
    DeployStatus,
    ManifestRecoveryEvidence,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expired(deadline: str) -> bool:
    return datetime.fromisoformat(deadline) <= datetime.now(timezone.utc)


class DeployAttemptStore(
    DeployAttemptGenerationStore,
    DeployAttemptProgressStore,
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
                if not manual_retry and retry is not None:
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
                if await self._retry_unlocked(deploy_id) is not None:
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
                if probe["status"] == "begun" and probe["proposal_fingerprint"] == proposal.fingerprint:
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
                stale = await self._proposal_stale_reason(probe, proposal)
                if stale:
                    reason, error = stale
                    await self._terminalize_probe_unlocked(
                        probe, "preflight_stale", "proposal", reason, error
                    )
                    await self._db.commit()
                    return {"status": "terminal", "kind": "preflight_stale"}
                await self._db.execute(
                    "UPDATE deploy_plan_probes SET status = 'proposed', mode = ?, "
                    "proposal_fingerprint = ?, proposal_json = ? WHERE probe_id = ?",
                    (proposal.mode, proposal.fingerprint, proposal.model_dump_json(), proposal.probe_id),
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

    async def record_result(
        self,
        *,
        deploy_id: str,
        orchestrator_attempt_id: str,
        connection_generation: str,
        status: str,
        error: str | None,
        duration_ms: int | None,
    ) -> dict[str, Any]:
        async with self._mutation_lock:
            try:
                attempt = await self._current_attempt(
                    deploy_id, orchestrator_attempt_id, connection_generation
                )
                if attempt is None:
                    return {"status": "ignored"}
                if attempt["execution_mode"] != "execute":
                    result = await self._fail_attempt_unlocked(
                        attempt=attempt,
                        kind="execution_mode_mismatch",
                        stage="message_validation",
                        reason="result_forbidden_for_mode",
                        error="raw DeployResult is forbidden for evidence_recovery mode",
                    )
                elif status == "failed":
                    await self._db.execute(
                        "UPDATE deploy_attempts SET result_status = ?, result_error = ?, "
                        "duration_ms = ? WHERE orchestrator_attempt_id = ?",
                        (status, error, duration_ms, orchestrator_attempt_id),
                    )
                    result = await self._fail_attempt_unlocked(
                        attempt={**attempt, "duration_ms": duration_ms},
                        kind="deploy_result_failed",
                        stage="execution",
                        reason="deploy_result_failed",
                        error=error or "deployment operation failed",
                    )
                else:
                    await self._db.execute(
                        "UPDATE deploy_attempts SET result_status = 'success', result_error = NULL, "
                        "duration_ms = ? WHERE orchestrator_attempt_id = ?",
                        (duration_ms, orchestrator_attempt_id),
                    )
                    attempt = await self._attempt(orchestrator_attempt_id)
                    result = await self._finalize_normal_if_ready(attempt)
                await self._db.commit()
                return result
            except Exception:
                await self._db.rollback()
                raise

    async def record_settled(
        self,
        *,
        deploy_id: str,
        orchestrator_attempt_id: str,
        connection_generation: str,
        local_head: str,
        remote_head: str,
    ) -> dict[str, Any]:
        async with self._mutation_lock:
            try:
                attempt = await self._current_attempt(
                    deploy_id, orchestrator_attempt_id, connection_generation
                )
                if attempt is None:
                    return {"status": "ignored"}
                await self._db.execute(
                    "UPDATE deploy_attempts SET settled_local_head = ?, settled_remote_head = ?, "
                    "settled_at = ? WHERE orchestrator_attempt_id = ?",
                    (local_head, remote_head, _now_iso(), orchestrator_attempt_id),
                )
                attempt = await self._attempt(orchestrator_attempt_id)
                if attempt["execution_mode"] != "execute":
                    result = {"status": "recorded"}
                elif local_head != remote_head or local_head != attempt["target_head"]:
                    result = await self._fail_attempt_unlocked(
                        attempt=attempt,
                        kind="settled_head_mismatch",
                        stage="reconciliation",
                        reason="settled_head_mismatch",
                        error="settled HEAD mismatch: "
                        f"local={local_head} remote={remote_head} target={attempt['target_head']}",
                    )
                else:
                    result = await self._finalize_normal_if_ready(attempt)
                await self._db.commit()
                return result
            except Exception:
                await self._db.rollback()
                raise

    async def record_recovery_evidence(
        self, evidence: ManifestRecoveryEvidence
    ) -> dict[str, Any]:
        async with self._mutation_lock:
            try:
                attempt = await self._current_attempt(
                    evidence.deploy_id,
                    evidence.orchestrator_attempt_id,
                    evidence.connection_generation,
                )
                if attempt is None:
                    return {"status": "ignored"}
                if attempt["execution_mode"] != "evidence_recovery":
                    result = await self._fail_attempt_unlocked(
                        attempt=attempt,
                        kind="execution_mode_mismatch",
                        stage="message_validation",
                        reason="evidence_forbidden_for_mode",
                        error="ManifestRecoveryEvidence is forbidden for execute mode",
                    )
                else:
                    error = await self._validate_recovery_evidence(attempt, evidence)
                    if error:
                        result = await self._fail_attempt_unlocked(
                            attempt=attempt,
                            kind="recovery_evidence_invalid",
                            stage="evidence_validation",
                            reason="recovery_evidence_invalid",
                            error=error,
                        )
                    else:
                        result = await self._success_unlocked(
                            attempt, "evidence_recovered_success"
                        )
                await self._db.commit()
                return result
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
                )
                await self._db.commit()
                return result
            except Exception:
                await self._db.rollback()
                raise

    async def record_node_terminal(
        self, terminal: DeployAttemptTerminal
    ) -> dict[str, Any]:
        """Apply the only node-authorized terminal to its exact active owner."""
        async with self._mutation_lock:
            try:
                attempt = await self._current_attempt(
                    terminal.deploy_id,
                    terminal.orchestrator_attempt_id,
                    terminal.connection_generation,
                )
                if attempt is None:
                    return {"status": "ignored"}
                result = await self._fail_attempt_unlocked(
                    attempt=attempt,
                    kind=terminal.kind,
                    stage=terminal.stage,
                    reason=terminal.reason,
                    error=terminal.error,
                )
                await self._db.commit()
                return {**result, "node_id": attempt["node_id"]}
            except Exception:
                await self._db.rollback()
                raise

    async def get_attempt(
        self, orchestrator_attempt_id: str
    ) -> dict[str, Any] | None:
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
