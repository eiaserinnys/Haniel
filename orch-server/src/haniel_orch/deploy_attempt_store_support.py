"""Internal SQL and transition support for durable deploy attempts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from .protocol import DeployPlanProposal, DeployStatus, ManifestRecoveryEvidence


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expired(deadline: str) -> bool:
    return datetime.fromisoformat(deadline) <= datetime.now(timezone.utc)


class DeployAttemptStoreSupport:
    """Private helpers mixed into the transaction-owning store."""

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
                    "dependent_readiness_failures": json.loads(
                        attempt["dependent_readiness_failures_json"]
                    ),
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
                    "dependent_readiness_failures": json.loads(
                        attempt["dependent_readiness_failures_json"]
                    ),
                },
            )
        return metadata

    async def cleanup_retry(self, deploy_id: str) -> None:
        await self._db.execute(
            "DELETE FROM deploy_retry_requirements WHERE deploy_id = ?", (deploy_id,)
        )
        await self._db.execute(
            "DELETE FROM deploy_retry_source_attempts WHERE deploy_id = ?", (deploy_id,)
        )

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
        await self._db.execute(
            """INSERT INTO deploy_attempts
               (orchestrator_attempt_id, deploy_id, node_id, repo, branch, target_head,
                source, execution_mode, approved_by, probe_id, connection_generation,
                proposal_fingerprint, source_orchestrator_attempt_id, journal_attempt_id,
                manifest_identity, manifest_digest, journal_target_head,
                journal_completed_at, original_previous_head, commits_json,
                affected_services_json, diff_stat, detected_at, started_at, deadline_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                orchestrator_attempt_id,
                event["deploy_id"],
                event["node_id"],
                event["repo"],
                event["branch"],
                event["target_head"] or event["deploy_id"].rsplit(":", 1)[-1],
                source,
                mode,
                approved_by,
                probe and probe["probe_id"],
                connection_generation,
                proposal and proposal.fingerprint,
                probe and probe["source_orchestrator_attempt_id"],
                proposal and proposal.journal_attempt_id,
                proposal and proposal.manifest_identity,
                proposal and proposal.manifest_digest,
                proposal and proposal.journal_target_head,
                proposal and proposal.journal_completed_at,
                proposal and proposal.original_previous_head,
                event["commits_json"],
                event["affected_services_json"],
                event["diff_stat"],
                event["detected_at"],
                now,
                deadline_at,
            ),
        )
        updated = await self._db.execute(
            "UPDATE deploy_events SET status = ?, approved_by = ?, updated_at = ? "
            "WHERE deploy_id = ? AND status = ?",
            (
                DeployStatus.DEPLOYING.value,
                approved_by,
                now,
                event["deploy_id"],
                DeployStatus.PENDING.value,
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError("canonical owner changed during begin")

    async def _finalize_normal_if_ready(
        self, attempt: dict[str, Any]
    ) -> dict[str, Any]:
        if (
            attempt["result_status"] == "success"
            and attempt["settled_local_head"] == attempt["target_head"]
            and attempt["settled_remote_head"] == attempt["target_head"]
        ):
            return await self._success_unlocked(attempt, "success")
        return {"status": "recorded"}

    async def _success_unlocked(
        self, attempt: dict[str, Any], outcome: str
    ) -> dict[str, Any]:
        now = _now_iso()
        changed = await self._db.execute(
            "UPDATE deploy_attempts SET outcome = ?, completed_at = ? "
            "WHERE orchestrator_attempt_id = ? AND outcome = 'active'",
            (outcome, now, attempt["orchestrator_attempt_id"]),
        )
        if changed.rowcount != 1:
            return {"status": "ignored"}
        await self._db.execute(
            "UPDATE deploy_events SET status = ?, error = NULL, duration_ms = ?, updated_at = ? "
            "WHERE deploy_id = ? AND status = ?",
            (
                DeployStatus.SUCCESS.value,
                attempt["duration_ms"],
                now,
                attempt["deploy_id"],
                DeployStatus.DEPLOYING.value,
            ),
        )
        await self.cleanup_retry(attempt["deploy_id"])
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
    ) -> dict[str, Any]:
        now = _now_iso()
        changed = await self._db.execute(
            "UPDATE deploy_attempts SET outcome = 'failed', terminal_kind = ?, "
            "terminal_stage = ?, terminal_reason = ?, terminal_error = ?, "
            "completed_at = ? WHERE orchestrator_attempt_id = ? "
            "AND outcome = 'active'",
            (kind, stage, reason, error, now, attempt["orchestrator_attempt_id"]),
        )
        if changed.rowcount != 1:
            return {"status": "ignored"}
        event = await self._event(attempt["deploy_id"])
        if event is None:
            return {"status": "failed", "deploy_id": attempt["deploy_id"]}
        if not await self._is_latest(event):
            await self._db.execute(
                "UPDATE deploy_events SET status = ?, reject_reason = ?, updated_at = ? "
                "WHERE deploy_id = ?",
                (
                    DeployStatus.REJECTED.value,
                    "superseded by newer canonical",
                    now,
                    attempt["deploy_id"],
                ),
            )
            await self.cleanup_retry(attempt["deploy_id"])
            await self._terminalize_sibling_probes_unlocked(
                attempt["deploy_id"],
                "canonical was superseded before this probe began",
            )
            return {"status": "superseded", "deploy_id": attempt["deploy_id"]}
        await self._db.execute(
            "UPDATE deploy_events SET status = ?, approved_by = NULL, error = NULL, "
            "duration_ms = NULL, "
            "updated_at = ? WHERE deploy_id = ? AND status = ?",
            (
                DeployStatus.PENDING.value,
                now,
                attempt["deploy_id"],
                DeployStatus.DEPLOYING.value,
            ),
        )
        current_retry = await self._retry_unlocked(attempt["deploy_id"])
        stable_source = (
            current_retry["source_orchestrator_attempt_id"]
            if attempt["execution_mode"] == "evidence_recovery" and current_retry
            else attempt["orchestrator_attempt_id"]
        )
        created_at = current_retry["created_at"] if current_retry else now
        await self._db.execute(
            """INSERT INTO deploy_retry_requirements
               (deploy_id, source_orchestrator_attempt_id,
                last_failed_orchestrator_attempt_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(deploy_id) DO UPDATE SET
                 source_orchestrator_attempt_id=excluded.source_orchestrator_attempt_id,
                 last_failed_orchestrator_attempt_id=excluded.last_failed_orchestrator_attempt_id,
                 updated_at=excluded.updated_at""",
            (
                attempt["deploy_id"],
                stable_source,
                attempt["orchestrator_attempt_id"],
                created_at,
                now,
            ),
        )
        await self._db.execute(
            "INSERT OR IGNORE INTO deploy_retry_source_attempts VALUES (?, ?, ?)",
            (attempt["deploy_id"], attempt["orchestrator_attempt_id"], now),
        )
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

    async def _validate_recovery_evidence(
        self, attempt: dict[str, Any], evidence: ManifestRecoveryEvidence
    ) -> str | None:
        retry = await self._retry_unlocked(attempt["deploy_id"])
        checks = {
            "source lineage": retry is not None
            and evidence.source_orchestrator_attempt_id
            == retry["source_orchestrator_attempt_id"]
            and evidence.source_orchestrator_attempt_id in retry["lineage"],
            "journal identity": evidence.journal_attempt_id
            == attempt["journal_attempt_id"],
            "target": evidence.target_head
            == attempt["target_head"]
            == evidence.current_head,
            "manifest identity": evidence.manifest_identity
            == attempt["manifest_identity"],
            "manifest digest": evidence.manifest_digest == attempt["manifest_digest"],
            "journal completion": evidence.journal_completed_at
            == attempt["journal_completed_at"],
            "node": evidence.node_id == attempt["node_id"],
            "repo": evidence.repo == attempt["repo"],
            "branch": evidence.branch == attempt["branch"],
            "latest canonical": await self._is_latest(
                await self._event(attempt["deploy_id"])
            ),
        }
        failed = [name for name, value in checks.items() if not value]
        return f"recovery evidence mismatch: {', '.join(failed)}" if failed else None

    def _validate_recovery_proposal(
        self, probe: dict[str, Any], proposal: DeployPlanProposal
    ) -> str | None:
        lineage = json.loads(probe["retry_lineage_json"])
        checks = {
            "journal success": proposal.journal_status == "success",
            "journal_attempt_id": bool(proposal.journal_attempt_id),
            "linked source": proposal.linked_orchestrator_attempt_id
            == probe["source_orchestrator_attempt_id"],
            "source lineage": probe["source_orchestrator_attempt_id"] in lineage,
            "target": proposal.current_head
            == probe["target_head"]
            == proposal.journal_target_head,
            "manifest identity": proposal.manifest_identity
            == probe["expected_manifest_identity"],
            "manifest digest": proposal.manifest_digest
            == probe["expected_manifest_digest"],
            "completion timestamp": bool(proposal.journal_completed_at),
        }
        failed = [name for name, value in checks.items() if not value]
        return f"recovery proposal mismatch: {', '.join(failed)}" if failed else None

    @staticmethod
    def _validate_execute_proposal(
        probe: dict[str, Any], proposal: DeployPlanProposal
    ) -> str | None:
        """Fail closed when an equal-HEAD manifest retry lacks a safe journal."""
        expected_manifest = probe["expected_manifest_identity"] is not None
        if not expected_manifest:
            return None
        if proposal.current_head != probe["target_head"]:
            return None
        checks = {
            "failed journal": proposal.journal_status == "failed",
            "journal_attempt_id": bool(proposal.journal_attempt_id),
            "journal target": proposal.journal_target_head == probe["target_head"],
            "original previous_head": bool(proposal.original_previous_head),
            "linked source": proposal.linked_orchestrator_attempt_id
            == probe["source_orchestrator_attempt_id"],
            "source lineage": probe["source_orchestrator_attempt_id"]
            in json.loads(probe["retry_lineage_json"]),
            "manifest identity": proposal.manifest_identity
            == probe["expected_manifest_identity"],
            "manifest digest": proposal.manifest_digest
            == probe["expected_manifest_digest"],
            "completion timestamp": bool(proposal.journal_completed_at),
        }
        failed = [name for name, value in checks.items() if not value]
        return f"execute proposal mismatch: {', '.join(failed)}" if failed else None

    async def _proposal_stale_reason(
        self, probe: dict[str, Any], proposal: DeployPlanProposal
    ) -> tuple[str, str] | None:
        if _expired(probe["deadline_at"]):
            return "probe_deadline_expired", "probe deadline expired"
        if proposal.connection_generation != probe["connection_generation"]:
            return "connection_generation_changed", "connection generation changed"
        fields = ("deploy_id", "node_id", "repo", "branch", "target_head")
        if any(getattr(proposal, field) != probe[field] for field in fields):
            return "proposal_snapshot_changed", "proposal snapshot changed"
        event = await self._event(probe["deploy_id"])
        if (
            event is None
            or event["status"] != DeployStatus.PENDING.value
            or not await self._is_latest(event)
        ):
            return (
                "canonical_not_latest_pending",
                "canonical is no longer latest pending",
            )
        retry = await self._retry_unlocked(probe["deploy_id"])
        probe_source = probe["source_orchestrator_attempt_id"]
        probe_lineage = json.loads(probe["retry_lineage_json"])
        if probe_source is None and retry is not None:
            return (
                "retry_requires_manual_approval",
                "retry now requires manual approval",
            )
        if probe_source is not None and (
            retry is None
            or retry["source_orchestrator_attempt_id"] != probe_source
            or retry["lineage"] != probe_lineage
        ):
            return "retry_lineage_changed", "retry lineage changed after probe creation"
        return None

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

    async def _current_attempt(
        self, deploy_id: str, orchestrator_attempt_id: str, generation: str
    ) -> dict[str, Any] | None:
        attempt = await self._attempt(orchestrator_attempt_id)
        if (
            attempt is None
            or attempt["deploy_id"] != deploy_id
            or attempt["connection_generation"] != generation
            or attempt["outcome"] != "active"
        ):
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
            {column[0]: value for column, value in zip(cursor.description, row)}
            for row in rows
        ]
