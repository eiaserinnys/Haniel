"""Read-only retry planning and approval revalidation for one repository."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .deployment import DeploymentStateStore
from .git import GitError, get_head, sha256_file_at_commit


@dataclass(frozen=True)
class RetryPlan:
    mode: Literal["execute", "evidence_recovery", "fail_closed"]
    evidence: dict[str, Any]
    reason: str
    error: str | None
    fingerprint: str

    def proposal(self, probe: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "deploy_plan_proposal",
            "mode": self.mode,
            "probe_id": probe["probe_id"],
            "connection_generation": probe["connection_generation"],
            "deploy_id": probe["deploy_id"],
            "node_id": probe["node_id"],
            "repo": probe["repo"],
            "branch": probe["branch"],
            "target_head": probe["target_head"],
            "reason": self.reason,
            "error": self.error,
            "fingerprint": self.fingerprint,
            **self.evidence,
        }


class DeployRetryPlanner:
    """Inspect Git and durable journal only; it has no mutation callbacks."""

    def __init__(
        self,
        *,
        repo_path: Path,
        manifest_path: str | None,
        journal_store: DeploymentStateStore,
        deployed_head: str | None = None,
    ) -> None:
        self.repo_path = repo_path
        self.manifest_path = manifest_path
        self.journal_store = journal_store
        self.deployed_head = deployed_head

    def plan(self, probe: dict[str, Any]) -> RetryPlan:
        current_head = self.deployed_head or get_head(self.repo_path)
        target = probe["target_head"]
        journal = self.journal_store.read(probe["repo"])
        identity, digest, manifest_error = self._manifest_snapshot(target)
        evidence = self._evidence(current_head, journal, identity, digest)

        if self.manifest_path is None:
            return self._build("execute", evidence, "legacy_retry", None)
        if manifest_error:
            return self._build(
                "fail_closed", evidence, "manifest_read_failed", manifest_error
            )
        if identity != probe.get("expected_manifest_identity") or digest != probe.get(
            "expected_manifest_digest"
        ):
            return self._build(
                "fail_closed",
                evidence,
                "manifest_mismatch",
                "target manifest identity or digest differs from canonical snapshot",
            )
        if current_head != target:
            return self._build("execute", evidence, "normal_pull", None)
        if not journal:
            return self._build(
                "fail_closed",
                evidence,
                "journal_missing",
                "manifest retry journal is missing",
            )
        if journal.get("target_head") != target:
            return self._build(
                "fail_closed",
                evidence,
                "journal_target_mismatch",
                "journal target differs from approval target",
            )
        journal_scope_matches = (
            journal.get("repo") == probe["repo"]
            and journal.get("node_id") == probe["node_id"]
            and journal.get("branch") == probe["branch"]
        )
        if not journal_scope_matches:
            return self._build(
                "fail_closed",
                evidence,
                "journal_scope_mismatch",
                "journal node, repo, or branch differs from the canonical probe",
            )
        if (
            journal.get("manifest_identity") != identity
            or journal.get("manifest_digest") != digest
        ):
            return self._build(
                "fail_closed",
                evidence,
                "journal_manifest_mismatch",
                "journal manifest identity or digest differs from the target manifest",
            )
        if journal.get("state") == "success":
            source = probe.get("source_orchestrator_attempt_id")
            lineage = probe.get("retry_lineage", [])
            if not journal.get("orchestrator_attempt_id"):
                return self._build(
                    "fail_closed",
                    evidence,
                    "journal_link_missing",
                    "success journal lacks orchestrator_attempt_id",
                )
            if (
                journal.get("orchestrator_attempt_id") != source
                or source not in lineage
            ):
                return self._build(
                    "fail_closed",
                    evidence,
                    "retry_lineage_mismatch",
                    "success journal is outside retry lineage",
                )
            if not journal.get("completed_at"):
                return self._build(
                    "fail_closed",
                    evidence,
                    "journal_completion_missing",
                    "success journal lacks completion timestamp",
                )
            return self._build(
                "evidence_recovery", evidence, "durable_local_success", None
            )
        if (
            journal.get("state") in {"failed", "verification_failed"}
            and journal.get("previous_head")
            and journal.get("completed_at")
            and journal.get("orchestrator_attempt_id")
            == probe.get("source_orchestrator_attempt_id")
            and journal.get("orchestrator_attempt_id") in probe.get("retry_lineage", [])
        ):
            return self._build("execute", evidence, "manifest_retry", None)
        return self._build(
            "fail_closed",
            evidence,
            "unsafe_journal",
            "manifest retry requires a success journal or terminal failure journal "
            "with original previous_head",
        )

    def revalidate(self, probe: dict[str, Any], approval: dict[str, Any]) -> RetryPlan:
        current = self.plan(probe)
        if current.fingerprint != approval.get("preflight_fingerprint"):
            return self._build(
                "fail_closed",
                current.evidence,
                "approval_revalidation_failed",
                "HEAD, journal, target, or manifest changed after preflight",
            )
        if current.mode != approval.get("execution_mode"):
            return self._build(
                "fail_closed",
                current.evidence,
                "approval_revalidation_failed",
                "preflight execution mode changed before execution",
            )
        return current

    def _manifest_snapshot(
        self, target: str
    ) -> tuple[str | None, str | None, str | None]:
        if self.manifest_path is None:
            return None, None, None
        try:
            digest = sha256_file_at_commit(self.repo_path, target, self.manifest_path)
        except GitError as exc:
            return self.manifest_path, None, str(exc)
        return self.manifest_path, digest, None

    @staticmethod
    def _evidence(
        current_head: str,
        journal: dict[str, Any] | None,
        identity: str | None,
        digest: str | None,
    ) -> dict[str, Any]:
        return {
            "current_head": current_head,
            "journal_status": journal and journal.get("state"),
            "journal_attempt_id": journal and journal.get("journal_attempt_id"),
            "linked_orchestrator_attempt_id": journal
            and journal.get("orchestrator_attempt_id"),
            "journal_target_head": journal and journal.get("target_head"),
            "original_previous_head": journal and journal.get("previous_head"),
            "manifest_identity": identity,
            "manifest_digest": digest,
            "journal_completed_at": journal and journal.get("completed_at"),
        }

    @staticmethod
    def _build(
        mode: Literal["execute", "evidence_recovery", "fail_closed"],
        evidence: dict[str, Any],
        reason: str,
        error: str | None,
    ) -> RetryPlan:
        raw = json.dumps(
            {"mode": mode, "evidence": evidence, "reason": reason},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return RetryPlan(
            mode=mode,
            evidence=evidence,
            reason=reason,
            error=error,
            fingerprint=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        )
