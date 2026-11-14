"""계획 답과 증거 회수의 재확인(저장소 조각). 2b-2에서 저장소 도우미 모듈의 크기를 지키려 옮겼다. 내용은 그대로다."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .protocol import DeployPlanProposal, DeployStatus, ManifestRecoveryEvidence


def _expired(deadline: str) -> bool:
    return datetime.fromisoformat(deadline) <= datetime.now(timezone.utc)


class DeployAttemptValidation:
    """Mixin: 계획 답과 증거 회수가 지금 요청 줄과 맞는지 확인한다."""

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
            "failed journal": proposal.journal_status
            in ("failed", "verification_failed"),
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
        self, probe: dict[str, Any], proposal: DeployPlanProposal, source: str
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
        # 점검 필요가 켜졌으면 원천이 단건 승인이고 자동이 아닌 탐침만 시도를 연다. 자동 탐침은 탐침의
        # requested_orchestrator_attempt_id가 있거나 원천이 auto인 것이다(둘이 어긋나면 자동으로 본다).
        # 일괄 탐침은 탐침 줄에 원천 칸이 없어 source 인자로 가린다
        is_auto = probe["requested_orchestrator_attempt_id"] is not None or source == "auto"
        if await self._lock_on(probe["deploy_id"]) and (
            is_auto or source != "manual_single"
        ):
            return (
                "retry_requires_manual_approval",
                "retry now requires manual approval",
            )
        retry = await self._retry_unlocked(probe["deploy_id"])
        current = (
            (retry["source_orchestrator_attempt_id"], retry["lineage"])
            if retry is not None
            else (None, [])
        )
        if current != (
            probe["source_orchestrator_attempt_id"],
            json.loads(probe["retry_lineage_json"]),
        ):
            return "retry_lineage_changed", "retry lineage changed after probe creation"
        return None
