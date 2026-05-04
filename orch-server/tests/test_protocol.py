"""Tests for protocol message models and parse_node_message."""

import pytest
from pydantic import ValidationError

from haniel_orch.protocol import (
    ChangeNotification,
    DeployApproval,
    DeployReject,
    DeployResult,
    DeployStatus,
    NodeHello,
    NodeMessage,
    NodeStatus,
    OrchestratorMessage,
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
        )
        assert msg.status == "failed"
        assert msg.error == "exit code 1"


class TestServerMessages:
    def test_deploy_approval(self):
        msg = DeployApproval(deploy_id="d1")
        assert msg.type == "deploy_approval"
        assert msg.approved_by == "dashboard"

    def test_deploy_approval_custom_approver(self):
        msg = DeployApproval(deploy_id="d1", approved_by="slack")
        assert msg.approved_by == "slack"

    def test_deploy_reject(self):
        msg = DeployReject(deploy_id="d1", reason="not ready")
        assert msg.type == "deploy_reject"
        assert msg.reason == "not ready"


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
        raw = '{"type":"deploy_result","deploy_id":"d1","node_id":"n1","status":"success"}'
        msg = parse_node_message(raw)
        assert isinstance(msg, DeployResult)
        assert msg.status == "success"

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
