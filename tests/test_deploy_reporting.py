"""Trigger-independent deploy result and reconciliation contracts."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from haniel.core.repo_reconciliation import RepoReconciliationSnapshot
from haniel.integrations.deploy_reporting import (
    DeployReporter,
    classify_deploy_result,
)


def snapshot(*, in_sync: bool) -> RepoReconciliationSnapshot:
    return RepoReconciliationSnapshot(
        node_id="node-a",
        repo="repo-a",
        branch="main",
        local_head="remote-head" if in_sync else "local-head",
        remote_head="remote-head",
        deploy_id="node-a:repo-a:main:remote-head",
    )


@pytest.mark.parametrize("trigger", ["auto_apply", "manual_single", "manual_batch"])
@pytest.mark.parametrize(
    ("operation_error", "in_sync", "expected_status", "pending_visible"),
    [
        (None, True, "success", False),
        (None, False, "failed", True),
        ("hook failed", True, "failed", False),
        ("hook failed", False, "failed", True),
    ],
)
def test_trigger_head_result_matrix_has_one_head_contract(
    trigger: str,
    operation_error: str | None,
    in_sync: bool,
    expected_status: str,
    pending_visible: bool,
) -> None:
    status, error = classify_deploy_result(snapshot(in_sync=in_sync), operation_error)
    assert status == expected_status, trigger
    assert (not in_sync) is pending_visible, trigger
    if not in_sync:
        assert "node=node-a" in error
        assert "repo=repo-a" in error
        assert "branch=main" in error
        assert "local_head=local-head" in error
        assert "remote_head=remote-head" in error


async def test_manual_success_return_with_mismatch_sends_failed_then_settled() -> None:
    sent: list[dict] = []

    async def send_json(message: dict) -> None:
        sent.append(message)

    handler = MagicMock(return_value=None)
    reporter = DeployReporter(
        node_id="node-a",
        approval_handler=handler,
        snapshot_handler=lambda repo, branch, deploy_id: snapshot(in_sync=False),
        send_json=send_json,
    )

    await reporter.handle_approval(
        {
            "deploy_id": "node-a:repo-a:main:remote-head",
            "orchestrator_attempt_id": "orch-1",
            "connection_generation": "generation-1",
            "execution_mode": "execute",
            "approved_by": "dashboard",
        }
    )

    assert [message["type"] for message in sent] == [
        "deploy_result",
        "repo_reconciliation",
    ]
    # Raw operation success and settled HEAD are independent evidence. The
    # server combines them and turns this mismatch into retryable failure.
    assert sent[0]["status"] == "success"
    assert sent[0]["orchestrator_attempt_id"] == "orch-1"
    assert sent[1]["phase"] == "settled"
    assert sent[1]["orchestrator_attempt_id"] == "orch-1"
    assert sent[1]["deploy_id"] == "node-a:repo-a:main:remote-head"


async def test_attempt_snapshot_id_never_enters_node_wire() -> None:
    async def send_json(_message: dict) -> None:
        raise AssertionError("invalid ID must be rejected before send")

    reporter = DeployReporter(
        node_id="node-a",
        approval_handler=None,
        snapshot_handler=None,
        send_json=send_json,
    )
    invalid = snapshot(in_sync=False)
    object.__setattr__(invalid, "deploy_id", "attempt:deadbeef")
    with pytest.raises(ValueError, match="server-only"):
        await reporter.send_reconciliation(invalid, "settled")
