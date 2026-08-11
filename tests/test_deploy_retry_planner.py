"""Read-only retry plan and generation-bound ACK contracts."""

import hashlib

import pytest

from haniel.core.deploy_retry_planner import DeployRetryPlanner
from haniel.core.deployment import DeploymentStateStore
from haniel.integrations.deploy_attempt_gate import (
    DeployAttemptGate,
    DeployPermissionError,
)


def probe(**overrides):
    value = {
        "probe_id": "p1",
        "connection_generation": "g1",
        "deploy_id": "n:r:main:target",
        "node_id": "n",
        "repo": "r",
        "branch": "main",
        "target_head": "target",
        "source_orchestrator_attempt_id": "source",
        "retry_lineage": ["source"],
        "expected_manifest_identity": "release.json",
        "expected_manifest_digest": hashlib.sha256(b"manifest").hexdigest(),
    }
    value.update(overrides)
    return value


class TestDeployRetryPlanner:
    def planner(self, tmp_path, monkeypatch, *, head="target"):
        monkeypatch.setattr(
            "haniel.core.deploy_retry_planner.get_head", lambda _path: head
        )
        monkeypatch.setattr(
            "haniel.core.deploy_retry_planner.sha256_file_at_commit",
            lambda _path, _commit, _relative: hashlib.sha256(b"manifest").hexdigest(),
        )
        return DeployRetryPlanner(
            repo_path=tmp_path / "repo",
            manifest_path="release.json",
            journal_store=DeploymentStateStore(tmp_path / "journals"),
        )

    def test_success_journal_selects_evidence_recovery(self, tmp_path, monkeypatch):
        planner = self.planner(tmp_path, monkeypatch)
        store = planner.journal_store
        store.begin(
            "r",
            "previous",
            "target",
            "release",
            orchestrator_attempt_id="source",
            node_id="n",
            branch="main",
            manifest_identity="release.json",
            manifest_digest=hashlib.sha256(b"manifest").hexdigest(),
        )
        store.transition("r", "success")

        plan = planner.plan(probe())
        assert plan.mode == "evidence_recovery"
        assert plan.evidence["journal_attempt_id"]

    def test_handover_journal_accepts_late_orchestrator_link_for_recovery(
        self, tmp_path, monkeypatch
    ):
        planner = self.planner(tmp_path, monkeypatch)
        store = planner.journal_store
        journal_attempt_id = store.begin_handover(
            "r",
            previous_head="previous",
            target_ref="target",
            manifest_identity="release.json",
            request_id="request-1",
            expected_operation="upgrade",
            branch="main",
            node_id="n",
        )
        store.bind_handover_target(
            "r",
            request_id="request-1",
            target_head="target",
            release_id="release",
            manifest_digest=hashlib.sha256(b"manifest").hexdigest(),
        )
        store.begin(
            "r",
            "previous",
            "target",
            "release",
            orchestrator_attempt_id="source",
            node_id="n",
            branch="main",
            manifest_identity="release.json",
            manifest_digest=hashlib.sha256(b"manifest").hexdigest(),
            journal_attempt_id=journal_attempt_id,
            request_id="request-1",
            expected_operation="upgrade",
        )
        store.transition("r", "success")

        plan = planner.plan(probe())

        assert plan.mode == "evidence_recovery"
        assert plan.reason == "durable_local_success"

    def test_legacy_success_journal_without_orchestrator_link_fails_closed(
        self, tmp_path, monkeypatch
    ):
        planner = self.planner(tmp_path, monkeypatch)
        store = planner.journal_store
        store.begin(
            "r",
            "previous",
            "target",
            "release",
            node_id="n",
            branch="main",
            manifest_identity="release.json",
            manifest_digest=hashlib.sha256(b"manifest").hexdigest(),
        )
        store.transition("r", "success")

        plan = planner.plan(probe())
        assert plan.mode == "fail_closed"
        assert plan.reason == "journal_link_missing"

    def test_failed_journal_reuses_original_previous_head(self, tmp_path, monkeypatch):
        planner = self.planner(tmp_path, monkeypatch)
        store = planner.journal_store
        store.begin(
            "r",
            "original-previous",
            "target",
            "release",
            orchestrator_attempt_id="source",
            node_id="n",
            branch="main",
            manifest_identity="release.json",
            manifest_digest=hashlib.sha256(b"manifest").hexdigest(),
        )
        store.transition("r", "failed")

        plan = planner.plan(probe())
        assert plan.mode == "execute"
        assert plan.evidence["original_previous_head"] == "original-previous"

    def test_verification_failed_journal_retries_preserved_target(
        self, tmp_path, monkeypatch
    ):
        planner = self.planner(tmp_path, monkeypatch)
        store = planner.journal_store
        store.begin(
            "r",
            "original-previous",
            "target",
            "release",
            orchestrator_attempt_id="source",
            node_id="n",
            branch="main",
            manifest_identity="release.json",
            manifest_digest=hashlib.sha256(b"manifest").hexdigest(),
        )
        store.transition(
            "r",
            "verification_failed",
            message="health retries exhausted",
            error=RuntimeError("health retries exhausted"),
        )

        plan = planner.plan(probe())

        assert plan.mode == "execute"
        assert plan.reason == "manifest_retry"
        assert plan.evidence["journal_status"] == "verification_failed"

    def test_head_mismatch_uses_normal_pull_without_inheriting_old_journal(
        self, tmp_path, monkeypatch
    ):
        planner = self.planner(tmp_path, monkeypatch, head="old")
        plan = planner.plan(probe())
        assert plan.mode == "execute"
        assert plan.reason == "normal_pull"

    def test_digest_change_after_probe_fails_revalidation(self, tmp_path, monkeypatch):
        planner = self.planner(tmp_path, monkeypatch, head="old")
        initial = planner.plan(probe())
        approval = {
            "execution_mode": initial.mode,
            "preflight_fingerprint": initial.fingerprint,
        }
        monkeypatch.setattr(
            "haniel.core.deploy_retry_planner.sha256_file_at_commit",
            lambda _path, _commit, _relative: hashlib.sha256(b"changed").hexdigest(),
        )
        revalidated = planner.revalidate(probe(), approval)
        assert revalidated.mode == "fail_closed"
        assert revalidated.reason == "approval_revalidation_failed"


class TestDeployAttemptGate:
    def test_accepted_ack_is_the_only_execution_permission(self):
        gate = DeployAttemptGate()
        gate.observe_generation("g1")
        gate.register("a1", "d1")
        assert gate.accept_ack(
            {
                "type": "deploy_attempt_ack",
                "accepted": True,
                "requested_orchestrator_attempt_id": "a1",
                "begun_orchestrator_attempt_id": "a1",
                "deploy_id": "d1",
                "connection_generation": "g1",
                "probe_id": "p1",
                "execution_mode": "execute",
                "preflight_fingerprint": "fp",
            }
        )
        assert gate.wait("a1", 0.01)["accepted"] is True

    def test_rejected_ack_closes_future_immediately(self):
        gate = DeployAttemptGate()
        gate.observe_generation("g1")
        gate.register("a1", "d1")
        gate.accept_ack(
            {
                "type": "deploy_attempt_ack",
                "accepted": False,
                "requested_orchestrator_attempt_id": "a1",
                "deploy_id": "d1",
                "connection_generation": "g1",
                "error": "retry_requires_manual_approval",
            }
        )
        with pytest.raises(
            DeployPermissionError, match="retry_requires_manual_approval"
        ):
            gate.wait("a1", 10)

    def test_stale_generation_and_wrong_request_do_not_open_permission(self):
        gate = DeployAttemptGate()
        gate.observe_generation("g2")
        gate.register("current", "d1")
        assert not gate.accept_ack(
            {
                "type": "deploy_attempt_ack",
                "accepted": False,
                "requested_orchestrator_attempt_id": "old",
                "deploy_id": "d1",
                "connection_generation": "g1",
                "error": "retry_requires_manual_approval",
            }
        )

    def test_cross_contaminated_rejection_is_rejected(self):
        gate = DeployAttemptGate()
        gate.register("a1", "d1")
        with pytest.raises(DeployPermissionError, match="contaminated"):
            gate.accept_ack(
                {
                    "type": "deploy_attempt_ack",
                    "accepted": False,
                    "requested_orchestrator_attempt_id": "a1",
                    "deploy_id": "d1",
                    "connection_generation": "g1",
                    "error": "retry_requires_manual_approval",
                    "probe_id": "forbidden",
                }
            )

    def test_unknown_ack_field_is_rejected(self):
        gate = DeployAttemptGate()
        gate.register("a1", "d1")
        with pytest.raises(DeployPermissionError, match="contaminated"):
            gate.accept_ack(
                {
                    "type": "deploy_attempt_ack",
                    "accepted": False,
                    "requested_orchestrator_attempt_id": "a1",
                    "deploy_id": "d1",
                    "connection_generation": "g1",
                    "error": "retry_requires_manual_approval",
                    "request_id": "forbidden-alias",
                }
            )

    def test_duplicate_rejection_is_idempotent(self):
        gate = DeployAttemptGate()
        gate.observe_generation("g1")
        gate.register("a1", "d1")
        ack = {
            "type": "deploy_attempt_ack",
            "accepted": False,
            "requested_orchestrator_attempt_id": "a1",
            "deploy_id": "d1",
            "connection_generation": "g1",
            "error": "retry_requires_manual_approval",
        }
        assert gate.accept_ack(ack)
        assert gate.accept_ack(ack)
        with pytest.raises(DeployPermissionError):
            gate.wait("a1", 0.01)

    def test_connection_generation_change_closes_current_future(self):
        gate = DeployAttemptGate()
        gate.observe_generation("g1")
        gate.register("a1", "d1")

        gate.observe_generation("g2")

        with pytest.raises(
            DeployPermissionError, match="connection_generation_changed"
        ):
            gate.wait("a1", 10)

    def test_reconnect_forgets_old_generation_before_new_request(self):
        gate = DeployAttemptGate()
        gate.observe_generation("old")
        gate.reset_connection()
        gate.register("new-attempt", "d1")
        gate.observe_generation("new")
        assert gate.accept_ack(
            {
                "type": "deploy_attempt_ack",
                "accepted": True,
                "requested_orchestrator_attempt_id": "new-attempt",
                "begun_orchestrator_attempt_id": "new-attempt",
                "deploy_id": "d1",
                "connection_generation": "new",
                "probe_id": "p-new",
                "execution_mode": "execute",
                "preflight_fingerprint": "fp-new",
            }
        )
        assert gate.wait("new-attempt", 0.01)["accepted"] is True
