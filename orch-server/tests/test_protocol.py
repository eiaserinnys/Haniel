"""Tests for protocol message models and parse_node_message."""

import json

import pytest
from pydantic import ValidationError

from haniel_orch.protocol import (
    AcceptedDeployAttemptAck,
    ChangeNotification,
    DeployApproval,
    DeployProgress,
    DeployReportAck,
    DeployAttemptTerminal,
    DeployPlanProposal,
    DeployReject,
    DeployResult,
    DeployStatus,
    NodeHello,
    NodeDeployReport,
    NodeStatus,
    RepoReconciliation,
    RejectedDeployAttemptAck,
    parse_node_message,
)


class TestDeployStatus:
    """DeployStatus enum tests."""

    def test_all_members_exist(self):
        assert DeployStatus.PENDING == "pending"
        assert DeployStatus.APPROVED == "approved"
        assert DeployStatus.REJECTED == "rejected"
        assert DeployStatus.DEPLOYING == "deploying"
        assert DeployStatus.SUCCESS == "success"
        assert DeployStatus.FAILED == "failed"

    def test_member_count(self):
        assert len(DeployStatus) == 6

    def test_lookup_by_name(self):
        """DeployStatus['SUCCESS'] — used in hub.py for DeployResult handling."""
        assert DeployStatus["SUCCESS"] == DeployStatus.SUCCESS
        assert DeployStatus["FAILED"] == DeployStatus.FAILED

    def test_lookup_from_deploy_result_status(self):
        """Simulate hub.py: DeployStatus[result.status.upper()]."""
        result_status = "success"
        assert DeployStatus[result_status.upper()] == DeployStatus.SUCCESS

        result_status = "failed"
        assert DeployStatus[result_status.upper()] == DeployStatus.FAILED


class TestNodeHello:
    def test_serialize_deserialize(self):
        msg = NodeHello(
            node_id="node-1",
            token="secret",
            hostname="server-01",
            os="Linux",
            arch="x86_64",
            haniel_version="0.14.2",
        )
        data = msg.model_dump()
        assert data["type"] == "node_hello"
        assert data["node_id"] == "node-1"
        assert data["token"] == "secret"

        restored = NodeHello.model_validate(data)
        assert restored == msg

    def test_json_roundtrip(self):
        msg = NodeHello(
            node_id="n1",
            token="t",
            hostname="h",
            os="Linux",
            arch="x86_64",
            haniel_version="0.1.0",
        )
        json_str = msg.model_dump_json()
        restored = NodeHello.model_validate_json(json_str)
        assert restored == msg


class TestChangeNotification:
    def test_serialize_deserialize(self):
        msg = ChangeNotification(
            deploy_id="node-1:myrepo:main:abc1234",
            node_id="node-1",
            repo="myrepo",
            branch="main",
            commits=["abc1234 fix: something", "def5678 feat: another"],
            affected_services=["bot", "mcp"],
            diff_stat="+10 -3",
            detected_at="2026-05-05T00:00:00+00:00",
        )
        data = msg.model_dump()
        assert data["type"] == "change_notification"
        assert data["deploy_id"] == "node-1:myrepo:main:abc1234"
        assert len(data["commits"]) == 2
        assert data["diff_stat"] == "+10 -3"

    def test_optional_diff_stat(self):
        msg = ChangeNotification(
            deploy_id="n:r:b:h",
            node_id="n",
            repo="r",
            branch="b",
            commits=["h msg"],
            affected_services=[],
            detected_at="2026-01-01T00:00:00Z",
        )
        assert msg.diff_stat is None

    def test_self_update_marker_is_optional_and_additive(self):
        legacy = ChangeNotification(
            deploy_id="n:r:b:h",
            node_id="n",
            repo="r",
            branch="b",
            commits=["h msg"],
            affected_services=[],
            detected_at="2026-01-01T00:00:00Z",
        )
        marked = legacy.model_copy(update={"is_self_update": True})

        assert legacy.is_self_update is None
        assert marked.model_dump()["is_self_update"] is True

    def test_manifest_identity_without_digest_is_a_fail_closed_snapshot(self):
        msg = ChangeNotification(
            deploy_id="n:r:b:h",
            node_id="n",
            repo="r",
            branch="b",
            commits=["h invalid manifest"],
            affected_services=[],
            detected_at="2026-08-01T00:00:00Z",
            deployment_kind="manifest",
            expected_manifest_identity="haniel.release.json",
        )
        assert msg.expected_manifest_digest is None

    def test_manifest_without_identity_is_rejected(self):
        with pytest.raises(ValidationError, match="requires identity"):
            ChangeNotification(
                deploy_id="n:r:b:h",
                node_id="n",
                repo="r",
                branch="b",
                commits=["h invalid manifest"],
                affected_services=[],
                detected_at="2026-08-01T00:00:00Z",
                deployment_kind="manifest",
            )


class TestNodeStatus:
    def test_serialize_deserialize(self):
        msg = NodeStatus(node_id="node-1")
        data = msg.model_dump()
        assert data["type"] == "node_status"
        assert data["node_id"] == "node-1"


class TestDeployResult:
    def test_success(self):
        msg = DeployResult(
            deploy_id="d1",
            node_id="n1",
            status="success",
            duration_ms=8200,
            orchestrator_attempt_id="a1",
            connection_generation="g1",
        )
        assert msg.error is None
        assert msg.duration_ms == 8200

    def test_failed_with_error(self):
        msg = DeployResult(
            deploy_id="d1",
            node_id="n1",
            status="failed",
            error="exit code 1",
            duration_ms=3400,
            orchestrator_attempt_id="a1",
            connection_generation="g1",
        )
        assert msg.status == "failed"
        assert msg.error == "exit code 1"


class TestNodeDeployReport:
    def test_parse_out_of_band_success_without_orchestrator_identity(self):
        msg = parse_node_message(
            json.dumps(
                {
                    "type": "node_deploy_report",
                    "phase": "succeeded",
                    "node_attempt_id": "node-attempt-1",
                    "journal_attempt_id": "journal-1",
                    "deploy_id": "n:r:main:new",
                    "node_id": "n",
                    "repo": "r",
                    "branch": "main",
                    "target_head": "new",
                    "local_head": "new",
                    "trigger": "startup",
                    "duration_ms": 15,
                }
            )
        )

        assert isinstance(msg, NodeDeployReport)
        assert msg.phase == "succeeded"
        assert "orchestrator_attempt_id" not in msg.model_dump()

    @pytest.mark.parametrize(
        ("phase", "error", "duration_ms"),
        [
            ("started", "unexpected", None),
            ("started", None, 10),
            ("succeeded", "unexpected", 10),
            ("failed", None, 10),
        ],
    )
    def test_phase_specific_evidence_is_strict(
        self, phase: str, error: str | None, duration_ms: int | None
    ):
        with pytest.raises(ValidationError):
            NodeDeployReport(
                phase=phase,
                node_attempt_id="node-attempt-1",
                deploy_id="n:r:main:new",
                node_id="n",
                repo="r",
                branch="main",
                target_head="new",
                local_head="new",
                trigger="local",
                error=error,
                duration_ms=duration_ms,
            )


class TestDeployProgress:
    def test_parse_progress_heartbeat(self):
        raw = (
            '{"type":"deploy_progress","deploy_id":"n:r:main:h",'
            '"node_id":"n","orchestrator_attempt_id":"a1",'
            '"connection_generation":"g1","stage":"build"}'
        )
        msg = parse_node_message(raw)
        assert isinstance(msg, DeployProgress)
        assert msg.stage == "build"

    def test_stage_inventory_is_closed(self):
        with pytest.raises(ValidationError):
            DeployProgress(
                deploy_id="n:r:main:h",
                node_id="n",
                orchestrator_attempt_id="a1",
                connection_generation="g1",
                stage="unknown",
            )


class TestRepoReconciliation:
    def test_parse_settled_snapshot(self):
        raw = (
            '{"type":"repo_reconciliation","phase":"settled",'
            '"deploy_id":"n:r:main:bbbb","node_id":"n","repo":"r",'
            '"branch":"main","local_head":"aaaa","remote_head":"bbbb",'
            '"orchestrator_attempt_id":"a1","connection_generation":"g1"}'
        )
        msg = parse_node_message(raw)
        assert isinstance(msg, RepoReconciliation)
        assert msg.phase == "settled"
        assert msg.local_head != msg.remote_head

    def test_settled_requires_connection_generation(self):
        with pytest.raises(ValidationError):
            RepoReconciliation(
                phase="settled",
                deploy_id="n:r:main:b",
                node_id="n",
                repo="r",
                branch="main",
                local_head="b",
                remote_head="b",
                orchestrator_attempt_id="a1",
            )

    def test_phase_is_closed_enum(self):
        with pytest.raises(ValidationError):
            RepoReconciliation(
                phase="invalid",
                deploy_id="n:r:main:b",
                node_id="n",
                repo="r",
                branch="main",
                local_head="a",
                remote_head="b",
            )


class TestServerMessages:
    def test_deploy_approval(self):
        msg = DeployApproval(
            deploy_id="d1",
            orchestrator_attempt_id="a1",
            execution_mode="execute",
            connection_generation="g1",
        )
        assert msg.type == "deploy_approval"
        assert msg.approved_by == "dashboard"

    def test_deploy_approval_custom_approver(self):
        msg = DeployApproval(
            deploy_id="d1",
            orchestrator_attempt_id="a1",
            execution_mode="execute",
            connection_generation="g1",
            approved_by="slack",
        )
        assert msg.approved_by == "slack"

    def test_deploy_reject(self):
        msg = DeployReject(deploy_id="d1", reason="not ready")
        assert msg.type == "deploy_reject"
        assert msg.reason == "not ready"

    def test_ignored_deploy_report_ack(self):
        msg = DeployReportAck(
            deploy_id="d1",
            orchestrator_attempt_id="a1",
            report_type="deploy_result",
            status="ignored",
            reason="terminal_attempt",
        )
        assert msg.type == "deploy_report_ack"
        assert msg.reason == "terminal_attempt"


class TestParseNodeMessage:
    def test_parse_node_hello(self):
        raw = '{"type":"node_hello","node_id":"n1","token":"t","hostname":"h","os":"Linux","arch":"x86_64","haniel_version":"0.1.0"}'
        msg = parse_node_message(raw)
        assert isinstance(msg, NodeHello)
        assert msg.node_id == "n1"

    def test_parse_change_notification(self):
        raw = '{"type":"change_notification","deploy_id":"n:r:b:h","node_id":"n","repo":"r","branch":"b","commits":["h msg"],"affected_services":["s1"],"detected_at":"2026-01-01T00:00:00Z"}'
        msg = parse_node_message(raw)
        assert isinstance(msg, ChangeNotification)
        assert msg.deploy_id == "n:r:b:h"

    def test_parse_node_status(self):
        raw = '{"type":"node_status","node_id":"n1"}'
        msg = parse_node_message(raw)
        assert isinstance(msg, NodeStatus)

    def test_parse_deploy_result(self):
        raw = '{"type":"deploy_result","deploy_id":"d1","node_id":"n1","status":"success","orchestrator_attempt_id":"a1","connection_generation":"g1"}'
        msg = parse_node_message(raw)
        assert isinstance(msg, DeployResult)
        assert msg.status == "success"

    @pytest.mark.parametrize("reason", ["planner_missing", "planner_error"])
    def test_parse_planner_fallback_fail_closed_proposal(self, reason: str):
        raw = json.dumps(
            {
                "type": "deploy_plan_proposal",
                "mode": "fail_closed",
                "probe_id": "p1",
                "connection_generation": "g1",
                "deploy_id": "n:r:main:target",
                "node_id": "n",
                "repo": "r",
                "branch": "main",
                "target_head": "target",
                "current_head": "",
                "reason": reason,
                "error": "planner could not produce a safe plan",
                "fingerprint": f"{reason}-fingerprint",
            }
        )

        proposal = parse_node_message(raw)

        assert isinstance(proposal, DeployPlanProposal)
        assert proposal.mode == "fail_closed"
        assert proposal.reason == reason

    def test_unknown_planner_reason_is_rejected(self):
        raw = json.dumps(
            {
                "type": "deploy_plan_proposal",
                "mode": "fail_closed",
                "probe_id": "p1",
                "connection_generation": "g1",
                "deploy_id": "n:r:main:target",
                "node_id": "n",
                "repo": "r",
                "branch": "main",
                "target_head": "target",
                "current_head": "",
                "reason": "unregistered_planner_reason",
                "fingerprint": "unknown-reason",
            }
        )

        with pytest.raises(ValidationError):
            parse_node_message(raw)


class TestDeployAttemptProtocolAuthority:
    def test_rejected_ack_has_only_requested_orchestrator_id(self):
        ack = RejectedDeployAttemptAck(
            accepted=False,
            requested_orchestrator_attempt_id="requested-a",
            deploy_id="d1",
            connection_generation="g1",
            error="retry_requires_manual_approval",
        )
        assert "request_id" not in ack.model_dump()

    def test_ack_variants_reject_cross_contamination(self):
        with pytest.raises(ValidationError):
            RejectedDeployAttemptAck(
                accepted=False,
                requested_orchestrator_attempt_id="requested-a",
                deploy_id="d1",
                connection_generation="g1",
                error="retry_requires_manual_approval",
                probe_id="forbidden",
            )
        with pytest.raises(ValidationError):
            AcceptedDeployAttemptAck(
                accepted=True,
                requested_orchestrator_attempt_id="a1",
                begun_orchestrator_attempt_id="a1",
                deploy_id="d1",
                connection_generation="g1",
                probe_id="p1",
                execution_mode="execute",
                preflight_fingerprint="fp",
                error="forbidden",
            )

    def test_node_terminal_authority_is_closed(self):
        with pytest.raises(ValidationError):
            DeployAttemptTerminal(
                kind="preflight_timeout",
                deploy_id="d1",
                orchestrator_attempt_id="a1",
                connection_generation="g1",
                reason="forbidden",
                error="forbidden",
            )

        with pytest.raises(ValidationError):
            DeployAttemptTerminal(
                kind="approval_revalidation_failed",
                deploy_id="d1",
                orchestrator_attempt_id="a1",
                connection_generation="g1",
                reason="untyped_reason",
                error="forbidden",
            )

    def test_missing_type_raises_value_error(self):
        with pytest.raises(ValueError, match="Missing 'type'"):
            parse_node_message('{"node_id":"n1"}')

    def test_unknown_type_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown message type"):
            parse_node_message('{"type":"unknown_type"}')

    def test_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError, match="Invalid JSON"):
            parse_node_message("not json")

    def test_non_object_json_raises_value_error(self):
        with pytest.raises(ValueError, match="Expected JSON object"):
            parse_node_message("[1, 2, 3]")

    def test_missing_required_field_raises_validation_error(self):
        # NodeHello without required 'token' field
        with pytest.raises(ValidationError):
            parse_node_message('{"type":"node_hello","node_id":"n1"}')
